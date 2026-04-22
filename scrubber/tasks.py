"""
Celery task: process a ScrubJob end-to-end.

Pipeline
--------
1.  Load ScrubJob → mark PROCESSING
2.  Open uploaded file from Django storage
3.  Parse & normalise all phone numbers (deduped)
4.  Credit pre-flight check (atomic, with row-lock)
5.  Process in batches through the DNC engine (real DB lookup)
6.  Write clean-numbers and DNC-numbers result CSVs to media storage
7.  Persist final counts + COMPLETED on the job
8.  Deduct credits atomically + create CreditTransaction record
9.  Send completion email

Error handling
--------------
Any unhandled exception marks the job FAILED.  No Celery retries — scrub
jobs are not idempotent (credits would be double-charged).
"""

import csv
import io
import logging
from contextlib import contextmanager

from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from billing.models import CreditTransaction
from .dnc import run_checks
from .models import ScrubJob
from .phone import extract_unique_numbers

logger = logging.getLogger(__name__)

BATCH_SIZE = getattr(settings, 'SCRUB_BATCH_SIZE', 300_000)

# Maps scrub_type key → column header used in DNC output CSV
_TYPE_LABEL = {
    'federal_dnc': 'Federal DNC',
    'state_dnc':   'State DNC',
    'litigator':   'Litigator',
}


# ── Helpers ──────────────────────────────────────────────────────────────────

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
        yield lst[i : i + size]


def _fmt(num: str) -> str:
    return f"({num[:3]}) {num[3:6]}-{num[6:]}"


def _build_clean_csv(clean_numbers: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\r\n')
    writer.writerow(['phone_number'])
    for num in clean_numbers:
        writer.writerow([_fmt(num)])
    return ('﻿' + buf.getvalue()).encode('utf-8')


def _build_dnc_csv(dnc_numbers: list, scrub_types: list) -> bytes:
    """
    Build DNC output CSV with dynamic column headers based on selected scrub types.

    dnc_numbers: list of (number_str, label, state_str) tuples
    scrub_types: list of selected scrub type keys e.g. ['federal_dnc', 'state_dnc']

    Column layout:
      - phone_number
      - One column per selected scrub type (e.g. "Federal DNC", "State DNC")
      - state  (always included — value blank for non-state-DNC hits)
    """
    selected_labels = [_TYPE_LABEL[t] for t in scrub_types if t in _TYPE_LABEL]

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\r\n')
    writer.writerow(['phone_number'] + selected_labels + ['state'])

    for num, hit_label, state_str in dnc_numbers:
        row = [_fmt(num)]
        for label in selected_labels:
            row.append('Yes' if label == hit_label else 'No')
        row.append(state_str or '')
        writer.writerow(row)

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
        f"  Federal DNC:    {job.dnc:,}\n"
        f"  State DNC:      {job.state_dnc:,}\n"
        f"  Litigators:     {job.litigator:,}\n\n"
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

    logger.info("Starting scrub job %s for user %s", job.job_id, job.user_id)

    with _job_error_guard(job):

        # ── 1. Mark processing ──────────────────────────────────────────
        _set_status(job, ScrubJob.Status.PROCESSING)

        # ── 2. Open uploaded file ───────────────────────────────────────
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

        # ── 3. Credit pre-flight ────────────────────────────────────────
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

        # ── 4. Batch processing ─────────────────────────────────────────
        scrub_types    = job.scrub_types or ['federal_dnc']
        all_clean:     list = []
        all_dnc:       list = []   # (number_str, hit_label, state_str)
        total_dnc      = 0
        total_state    = 0
        total_litigator = 0

        batches = list(_chunk(numbers, BATCH_SIZE))
        logger.info(
            "Job %s: processing %d batch(es) of up to %d numbers",
            job.job_id, len(batches), BATCH_SIZE,
        )

        for batch_idx, batch in enumerate(batches, start=1):
            logger.info(
                "Job %s: batch %d/%d — %d numbers",
                job.job_id, batch_idx, len(batches), len(batch),
            )

            result = run_checks(batch, scrub_types)

            all_clean.extend(result.clean)
            for n, (label, state_str) in result.dnc_details.items():
                all_dnc.append((n, label, state_str))

            total_dnc       += result.dnc_count
            total_state     += result.state_count
            total_litigator += result.litigator_count

            job.clean     = len(all_clean)
            job.dnc       = total_dnc
            job.state_dnc = total_state
            job.litigator = total_litigator
            job.save(update_fields=['clean', 'dnc', 'state_dnc', 'litigator'])

        # ── 5. Write result CSVs ────────────────────────────────────────
        job.result_file.save(
            f"{job.job_id}_clean.csv",
            ContentFile(_build_clean_csv(all_clean)),
            save=False,
        )
        job.result_file_dnc.save(
            f"{job.job_id}_dnc.csv",
            ContentFile(_build_dnc_csv(all_dnc, scrub_types)),
            save=False,
        )

        # ── 6. Persist final state ──────────────────────────────────────
        job.status    = ScrubJob.Status.COMPLETED
        job.total     = total
        job.clean     = len(all_clean)
        job.dnc       = total_dnc
        job.state_dnc = total_state
        job.litigator = total_litigator
        job.error_message = ''
        job.save(update_fields=[
            'status', 'total', 'clean', 'dnc',
            'state_dnc', 'litigator', 'result_file', 'result_file_dnc', 'error_message',
        ])

        logger.info(
            "Job %s COMPLETED — total=%d clean=%d dnc=%d litigator=%d state=%d",
            job.job_id, total, len(all_clean), total_dnc, total_litigator, total_state,
        )

        # ── 7. Deduct credits ───────────────────────────────────────────
        _deduct_credits(job, total)

        # ── 8. Send completion email ────────────────────────────────────
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
        'litigator': job.litigator,
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
