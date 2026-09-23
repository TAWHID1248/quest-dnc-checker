"""
Celery task: process a ScrubJob end-to-end.

Pipeline
--------
1.  Load ScrubJob → mark PROCESSING
2.  Parse uploaded file (CSV/TXT/XLSX) keeping full rows; on resume also
    load remaining numbers from the partial file
3.  Credit pre-flight check
4.  Start a background monitor thread that polls Redis every 300 ms for
    pause / cancel signals and sets a threading.Event immediately
5.  Process in chunks of CONTROL_CHECK_SIZE; _check_one skips new calls
    as soon as the stop_event is set, so queued calls halt within one
    API-call latency (~100 ms) of the user clicking pause
6.  Write clean and DNC result files (CSV or XLSX, all original columns
    kept, plus a dnc_status column: 'Clean' / 'Do Not Call')
7.  Persist final counts + COMPLETED
8.  Deduct credits atomically + create CreditTransaction record
9.  Send completion email

Pause / Resume / Cancel
-----------------------
- Redis key ``scrub_ctrl_{job.pk}`` carries the signal: 'pause' | 'cancel'
- Monitor thread sets a threading.Event within 300 ms of the key appearing
- dnc.run_checks receives the event; _check_one returns None (skip) when set
- BatchResult.unchecked holds numbers not sent to the API yet
- On pause: partial file = {clean, dnc, remaining} (unchecked + subsequent)
- On cancel: mark CANCELLED, delete partial file
- On resume: load remaining from partial file; the upload is re-parsed only
  to recover the original row data for the result files

Error handling
--------------
Any unhandled exception marks the job FAILED.  No Celery retries.
"""

import csv
import io
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction

from billing.models import CreditTransaction
from .dnc import run_checks
from .models import ScrubJob
from .phone import ParsedFile, cell_text, format_number, is_excel, parse_file

logger = logging.getLogger(__name__)

CONTROL_CHECK_SIZE = getattr(settings, 'SCRUB_CONTROL_CHECK_SIZE', 10_000)


# ── Control signal helpers ────────────────────────────────────────────────────

def _ctrl_key(job_pk: int) -> str:
    return f'scrub_ctrl_{job_pk}'


def _get_control(job_pk: int) -> str | None:
    return cache.get(_ctrl_key(job_pk))


def _clear_control(job_pk: int) -> None:
    cache.delete(_ctrl_key(job_pk))


def _control_monitor(job_pk: int, stop_event: threading.Event, interval: float = 0.3) -> None:
    """
    Background thread: poll Redis every `interval` seconds.
    Sets stop_event the moment a pause or cancel signal appears.
    Exits as soon as stop_event is set (either by us or by the main thread).
    """
    while not stop_event.is_set():
        if _get_control(job_pk) in ('pause', 'cancel'):
            stop_event.set()
            return
        time.sleep(interval)


# ── Partial-data persistence (pause / resume) ─────────────────────────────────

def _save_partial(
    job: ScrubJob,
    all_clean: list,
    all_dnc: list,
    remaining: list,
) -> None:
    """Serialise accumulated results + remaining work to storage."""
    payload = json.dumps({
        'clean':     all_clean,
        'dnc':       all_dnc,
        'remaining': remaining,
    }).encode('utf-8')

    if job.partial_data_file:
        try:
            job.partial_data_file.delete(save=False)
        except Exception:
            pass

    job.partial_data_file.save(
        f'{job.job_id}_partial.json',
        ContentFile(payload),
        save=True,
    )
    logger.info(
        "Saved partial data for job %s — %d clean, %d dnc, %d remaining",
        job.job_id, len(all_clean), len(all_dnc), len(remaining),
    )


def _load_partial(job: ScrubJob) -> tuple[list, list, list]:
    """Load partial file. Returns (clean, dnc, remaining)."""
    if not job.partial_data_file:
        return [], [], []
    try:
        job.partial_data_file.open('rb')
        data = json.loads(job.partial_data_file.read())
        job.partial_data_file.close()
        return data.get('clean', []), data.get('dnc', []), data.get('remaining', [])
    except Exception as exc:
        logger.warning("Could not load partial data for job %s: %s", job.job_id, exc)
        return [], [], []


def _delete_partial(job: ScrubJob) -> None:
    if job.partial_data_file:
        try:
            job.partial_data_file.delete(save=True)
        except Exception:
            pass


# ── Generic helpers ───────────────────────────────────────────────────────────

def _set_status(job: ScrubJob, status: str, **extra_fields) -> None:
    for attr, val in extra_fields.items():
        setattr(job, attr, val)
    job.status = status
    fields = ['status', 'error_message'] + list(extra_fields.keys())
    job.save(update_fields=list(dict.fromkeys(fields)))


@contextmanager
def _job_error_guard(job: ScrubJob):
    try:
        yield
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        logger.exception("ScrubJob %s failed: %s", job.job_id, msg)
        try:
            _set_status(job, ScrubJob.Status.FAILED, error_message=msg)
        except Exception:
            pass
        raise


def _upload_name(job: ScrubJob) -> str:
    """Name used to pick the parser/output format: prefer whichever carries the extension."""
    for name in (job.filename, job.file.name if job.file else ''):
        if name and '.' in name.rsplit('/', 1)[-1]:
            return name
    return job.filename or ''


def _rows_for(parsed: ParsedFile, numbers: list) -> list:
    """Full original rows for the given numbers (falls back to the bare number)."""
    out = []
    for num in numbers:
        row = parsed.rows.get(num)
        out.append(list(row) if row is not None else [format_number(num)])
    return out


def _build_csv(header: list, rows: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\r\n')
    writer.writerow(header)
    for row in rows:
        writer.writerow([cell_text(c) for c in row])
    return ('\ufeff' + buf.getvalue()).encode('utf-8')


def _xlsx_cell(value):
    """Keep native numbers/dates for Excel output; everything else as text."""
    if value is None or value == '':
        return None
    if isinstance(value, (int, float, datetime, date)) and not isinstance(value, bool):
        return value
    return cell_text(value)


def _build_xlsx(header: list, rows: list) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet('Results')
    ws.append(header)
    for row in rows:
        ws.append([_xlsx_cell(c) for c in row])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


STATUS_COLUMN = 'dnc_status'
STATUS_CLEAN   = 'Clean'
STATUS_DNC     = 'Do Not Call'


def _build_result_file(
    parsed: ParsedFile, numbers: list, excel: bool, status: str,
) -> tuple[str, bytes]:
    """
    Build a result file containing every original column for each number,
    plus a trailing ``dnc_status`` column ('Clean' or 'Do Not Call').

    Returns (extension, bytes). Excel uploads get an .xlsx back, everything
    else gets a UTF-8 (BOM) CSV.
    """
    base_header = parsed.output_header
    width = len(base_header)
    header = base_header + [STATUS_COLUMN]
    rows = []
    for row in _rows_for(parsed, numbers):
        row = row[:width] + [''] * (width - len(row))   # align status column
        rows.append(row + [status])
    if excel:
        return '.xlsx', _build_xlsx(header, rows)
    return '.csv', _build_csv(header, rows)


def _send_completion_email(job: ScrubJob) -> None:
    from django.conf import settings as django_settings
    from django.core.mail import send_mail

    user = job.user
    clean_rate = round((job.clean / job.total * 100), 1) if job.total else 0
    subject = f"DNC Scrub Complete — {job.job_id}"
    body = (
        f"Hello {user.display_name},\n\n"
        f"Your DNC scrub job {job.job_id} has completed successfully.\n\n"
        f"Results Summary\n"
        f"---------------\n"
        f"  File:           {job.filename}\n"
        f"  Total numbers:  {job.total:,}\n"
        f"  Clean:          {job.clean:,} ({clean_rate}%)\n"
        f"  DNC:            {job.dnc:,}\n\n"
        f"Log in to download your results:\n"
        f"https://app.checkdnc.net/scrubber/\n\n"
        f"— The CheckDNC Team"
    )
    send_mail(
        subject, body,
        django_settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=True,
    )
    logger.info("Sent completion email for job %s to %s", job.job_id, user.email)


def _deduct_credits(job: ScrubJob, amount: int) -> None:
    from django.contrib.auth import get_user_model
    User = get_user_model()

    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=job.user_id)
        if user.credits < amount:
            raise InsufficientCreditsError(
                f"Insufficient credits: need {amount}, have {user.credits}"
            )
        user.credits -= amount
        user.save(update_fields=['credits'])

    CreditTransaction.objects.create(
        user_id=job.user_id,
        type=CreditTransaction.Type.USAGE,
        amount=-amount,
        price=0,
        scrub_job=job,
    )
    logger.info("Deducted %d credits from user %s for job %s", amount, job.user_id, job.job_id)


# ── Custom exceptions ─────────────────────────────────────────────────────────

class InsufficientCreditsError(Exception):
    pass


class NoValidNumbersError(Exception):
    pass


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_scrub_job(job_id: int) -> dict:
    try:
        job = ScrubJob.objects.select_related('user').get(pk=job_id)
    except ScrubJob.DoesNotExist:
        logger.error("run_scrub_job called with unknown job_id=%s", job_id)
        raise

    is_resume = job.status == ScrubJob.Status.PAUSED
    logger.info(
        "%s scrub job %s for user %s",
        "Resuming" if is_resume else "Starting",
        job.job_id, job.user_id,
    )

    with _job_error_guard(job):

        # ── 1. Mark processing ──────────────────────────────────────
        _set_status(job, ScrubJob.Status.PROCESSING)

        # ── 2. Get the list of numbers to process ───────────────────
        # The upload is always re-parsed (also on resume) so every result row
        # can carry the user's original columns, not just the phone number.
        if not job.file:
            raise FileNotFoundError(f"No file attached to job {job.job_id}")
        try:
            job.file.open('rb')
            parsed = parse_file(job.file, _upload_name(job))
        finally:
            job.file.close()

        logger.info(
            "Job %s: parsed %d rows → %d unique valid numbers, %d invalid, %d duplicates",
            job.job_id, parsed.total_rows, len(parsed.numbers),
            parsed.invalid, parsed.duplicates,
        )

        if is_resume and job.partial_data_file:
            all_clean, all_dnc, remaining = _load_partial(job)
            total = job.total  # set during the first run
            logger.info(
                "Job %s resuming — %d remaining, %d clean, %d dnc already done",
                job.job_id, len(remaining), len(all_clean), len(all_dnc),
            )
        else:
            if not parsed.numbers:
                raise NoValidNumbersError(
                    f"File contained {parsed.total_rows} rows but zero valid US phone numbers."
                )

            total = len(parsed.numbers)
            all_clean, all_dnc = [], []
            remaining = parsed.numbers

            job.total = total
            job.save(update_fields=['total'])

        # ── 3. Credit pre-flight ────────────────────────────────────
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.get(pk=job.user_id)
        if user.credits < total:
            raise InsufficientCreditsError(
                f"Need {total} credits, balance is {user.credits}. "
                "Please top up and re-submit."
            )

        # ── 4. Start background control monitor ─────────────────────
        stop_event = threading.Event()
        monitor = threading.Thread(
            target=_control_monitor,
            args=(job.pk, stop_event),
            daemon=True,
        )
        monitor.start()

        scrub_types = job.scrub_types or ['federal_dnc']

        # ── 5. Process in chunks, checking stop_event each time ─────
        try:
            while remaining:
                chunk = remaining[:CONTROL_CHECK_SIZE]

                result = run_checks(chunk, scrub_types, stop_event=stop_event)

                all_clean.extend(result.clean)
                all_dnc.extend(result.dnc_numbers)

                if stop_event.is_set():
                    # Build what's truly left: skipped numbers from this chunk
                    # + everything after this chunk
                    still_remaining = result.unchecked + remaining[CONTROL_CHECK_SIZE:]

                    ctrl = _get_control(job.pk)

                    job.clean = len(all_clean)
                    job.dnc = len(all_dnc)
                    job.processed_count = total - len(still_remaining)
                    job.save(update_fields=['clean', 'dnc', 'processed_count'])

                    if ctrl == 'cancel':
                        _delete_partial(job)
                        _set_status(job, ScrubJob.Status.CANCELLED)
                        _clear_control(job.pk)
                        logger.info("Job %s cancelled", job.job_id)
                        return {'job_id': job.job_id, 'status': 'cancelled'}

                    # Default: treat as pause (covers both explicit 'pause' and
                    # the edge case where the key expired before we read it)
                    _save_partial(job, all_clean, all_dnc, still_remaining)
                    _set_status(
                        job, ScrubJob.Status.PAUSED,
                        processed_count=total - len(still_remaining),
                    )
                    _clear_control(job.pk)
                    logger.info(
                        "Job %s paused — %d remaining", job.job_id, len(still_remaining),
                    )
                    return {'job_id': job.job_id, 'status': 'paused'}

                # Full chunk completed — advance
                remaining = remaining[CONTROL_CHECK_SIZE:]

                job.clean = len(all_clean)
                job.dnc = len(all_dnc)
                job.processed_count = total - len(remaining)
                job.save(update_fields=['clean', 'dnc', 'processed_count'])

        finally:
            stop_event.set()   # always stop the monitor thread
            monitor.join(timeout=2)

        # ── 6. Write result files (same format as the upload) ───────
        excel_out = is_excel(_upload_name(job))
        ext, clean_bytes = _build_result_file(parsed, all_clean, excel_out, STATUS_CLEAN)
        job.result_file.save(
            f"{job.job_id}_clean{ext}",
            ContentFile(clean_bytes),
            save=False,
        )
        ext, dnc_bytes = _build_result_file(parsed, all_dnc, excel_out, STATUS_DNC)
        job.result_file_dnc.save(
            f"{job.job_id}_dnc{ext}",
            ContentFile(dnc_bytes),
            save=False,
        )

        # ── 7. Persist final state ──────────────────────────────────
        job.status = ScrubJob.Status.COMPLETED
        job.total = total
        job.clean = len(all_clean)
        job.dnc = len(all_dnc)
        job.state_dnc = 0
        job.processed_count = total
        job.error_message = ''
        job.save(update_fields=[
            'status', 'total', 'clean', 'dnc',
            'state_dnc', 'processed_count',
            'result_file', 'result_file_dnc', 'error_message',
        ])

        logger.info(
            "Job %s COMPLETED — total=%d clean=%d dnc=%d",
            job.job_id, total, len(all_clean), len(all_dnc),
        )

        _delete_partial(job)

        # ── 8. Deduct credits ───────────────────────────────────────
        _deduct_credits(job, total)

        # ── 9. Completion email ─────────────────────────────────────
        try:
            _send_completion_email(job)
        except Exception:
            logger.exception("Failed to send completion email for job %s", job.job_id)

    return {
        'job_id':    job.job_id,
        'status':    job.status,
        'total':     job.total,
        'clean':     job.clean,
        'dnc':       job.dnc,
        'state_dnc': job.state_dnc,
    }


@shared_task(
    bind=True,
    name='scrubber.tasks.process_scrub_job',
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
    time_limit=3600,
    soft_time_limit=3300,
)
def process_scrub_job(self, job_id: int) -> dict:
    return run_scrub_job(job_id)
