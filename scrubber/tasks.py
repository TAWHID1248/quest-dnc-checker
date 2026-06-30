"""
Celery task: process a ScrubJob end-to-end.

Pipeline
--------
1.  Load ScrubJob → mark PROCESSING
2.  Open uploaded file from Django storage
3.  Parse & normalise all phone numbers (deduped)
4.  Credit pre-flight check (fast, non-atomic)
5.  Process in chunks of CONTROL_CHECK_SIZE through the DNC API
    — after each chunk, check Redis for pause / cancel signals
6.  Write clean-numbers and DNC-numbers result CSVs to media storage
7.  Persist final counts + COMPLETED on the job
8.  Deduct credits atomically + create CreditTransaction record
9.  Send completion email

Pause / Resume / Cancel
-----------------------
- A Redis cache key ``scrub_ctrl_{job.pk}`` carries the signal: 'pause' | 'cancel'
- Checked after every CONTROL_CHECK_SIZE numbers (default 10 000)
- On pause: serialise accumulated results to partial_data_file, mark PAUSED, exit
- On cancel: mark CANCELLED, delete any partial file, exit
- On resume (job.status == PAUSED): load partial data, re-parse file, skip already-
  processed numbers, continue from where processing left off

Error handling
--------------
Any unhandled exception marks the job FAILED.  No Celery retries — scrub
jobs are not idempotent (credits would be double-charged).
"""

import csv
import io
import json
import logging
from contextlib import contextmanager

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction

from billing.models import CreditTransaction
from .dnc import run_checks
from .models import ScrubJob
from .phone import extract_unique_numbers

logger = logging.getLogger(__name__)

CONTROL_CHECK_SIZE = getattr(settings, 'SCRUB_CONTROL_CHECK_SIZE', 10_000)


# ── Control signal helpers ────────────────────────────────────────────────────

def _ctrl_key(job_pk: int) -> str:
    return f'scrub_ctrl_{job_pk}'


def _get_control(job_pk: int) -> str | None:
    return cache.get(_ctrl_key(job_pk))


def _clear_control(job_pk: int) -> None:
    cache.delete(_ctrl_key(job_pk))


# ── Partial-data persistence (pause / resume) ─────────────────────────────────

def _save_partial(job: ScrubJob, all_clean: list, all_dnc: list) -> None:
    payload = json.dumps({'clean': all_clean, 'dnc': all_dnc}).encode('utf-8')
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
    logger.info("Saved partial data for job %s (%d clean, %d dnc)", job.job_id, len(all_clean), len(all_dnc))


def _load_partial(job: ScrubJob) -> tuple[list, list]:
    if not job.partial_data_file:
        return [], []
    try:
        job.partial_data_file.open('rb')
        data = json.loads(job.partial_data_file.read())
        job.partial_data_file.close()
        return data.get('clean', []), data.get('dnc', [])
    except Exception as exc:
        logger.warning("Could not load partial data for job %s: %s", job.job_id, exc)
        return [], []


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


def _chunk(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i: i + size]


def _fmt(num: str) -> str:
    return f"({num[:3]}) {num[3:6]}-{num[6:]}"


def _build_clean_csv(clean_numbers: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\r\n')
    writer.writerow(['phone_number'])
    for num in clean_numbers:
        writer.writerow([_fmt(num)])
    return ('﻿' + buf.getvalue()).encode('utf-8')


def _build_dnc_csv(dnc_numbers: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\r\n')
    writer.writerow(['phone_number'])
    for num in dnc_numbers:
        writer.writerow([_fmt(num)])
    return ('﻿' + buf.getvalue()).encode('utf-8')


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
        f"https://checkdnc.net/scrubber/\n\n"
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

        # ── 2. Open and parse uploaded file ────────────────────────
        if not job.file:
            raise FileNotFoundError(f"No file attached to job {job.job_id}")

        try:
            job.file.open('rb')
            numbers, total_lines, invalid_count = extract_unique_numbers(job.file)
        finally:
            job.file.close()

        logger.info(
            "Job %s: parsed %d lines → %d unique valid numbers, %d invalid",
            job.job_id, total_lines, len(numbers), invalid_count,
        )

        if not numbers:
            raise NoValidNumbersError(
                f"File contained {total_lines} lines but zero valid US phone numbers."
            )

        total = len(numbers)

        # ── 3. Credit pre-flight ────────────────────────────────────
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.get(pk=job.user_id)

        if user.credits < total:
            raise InsufficientCreditsError(
                f"Need {total} credits, balance is {user.credits}. "
                "Please top up and re-submit."
            )

        job.total = total
        job.save(update_fields=['total'])

        # ── 4. Load partial data on resume ──────────────────────────
        if is_resume and job.processed_count > 0:
            all_clean, all_dnc = _load_partial(job)
            start_from = job.processed_count
            logger.info(
                "Job %s resuming from offset %d (%d clean, %d dnc loaded)",
                job.job_id, start_from, len(all_clean), len(all_dnc),
            )
        else:
            all_clean, all_dnc = [], []
            start_from = 0

        # ── 5. Process in control-check-sized chunks ────────────────
        scrub_types = job.scrub_types or ['federal_dnc']
        remaining = numbers[start_from:]
        processed_offset = start_from

        logger.info(
            "Job %s: %d numbers to process (starting at offset %d)",
            job.job_id, len(remaining), start_from,
        )

        for chunk in _chunk(remaining, CONTROL_CHECK_SIZE):
            result = run_checks(chunk, scrub_types)
            all_clean.extend(result.clean)
            all_dnc.extend(result.dnc_numbers)
            processed_offset += len(chunk)

            job.clean = len(all_clean)
            job.dnc = len(all_dnc)
            job.state_dnc = 0
            job.processed_count = processed_offset
            job.save(update_fields=['clean', 'dnc', 'state_dnc', 'processed_count'])

            ctrl = _get_control(job.pk)

            if ctrl == 'pause':
                _save_partial(job, all_clean, all_dnc)
                _set_status(job, ScrubJob.Status.PAUSED, processed_count=processed_offset)
                _clear_control(job.pk)
                logger.info("Job %s paused at offset %d", job.job_id, processed_offset)
                return {'job_id': job.job_id, 'status': 'paused'}

            if ctrl == 'cancel':
                _delete_partial(job)
                _set_status(job, ScrubJob.Status.CANCELLED)
                _clear_control(job.pk)
                logger.info("Job %s cancelled at offset %d", job.job_id, processed_offset)
                return {'job_id': job.job_id, 'status': 'cancelled'}

        # ── 6. Write result CSVs ────────────────────────────────────
        job.result_file.save(
            f"{job.job_id}_clean.csv",
            ContentFile(_build_clean_csv(all_clean)),
            save=False,
        )
        job.result_file_dnc.save(
            f"{job.job_id}_dnc.csv",
            ContentFile(_build_dnc_csv(all_dnc)),
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

        # Clean up partial file (present if this was a resumed job)
        _delete_partial(job)

        # ── 8. Deduct credits ───────────────────────────────────────
        _deduct_credits(job, total)

        # ── 9. Send completion email ────────────────────────────────
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
