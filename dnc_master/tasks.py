"""
Celery task: bulk-load a DNC master CSV/Excel file into dnc_master_numbers.

Flow
----
1. Open the DncUploadJob file from Django storage
2. Detect file type (CSV / Excel) and locate phone + state columns
3. If mode=replace: DELETE existing rows for this list_type
4. Stream file in chunks of CHUNK_SIZE rows
5. Normalize each phone number; skip invalid ones
6. Bulk-insert via PostgreSQL COPY (fastest possible path)
7. Update DncMasterList record count + last_updated
8. Mark DncUploadJob COMPLETED (or FAILED on error)
"""

import csv
import io
import logging
import re

from celery import shared_task
from django.db import connection
from django.utils import timezone

from .models import DncMasterList, DncUploadJob, LIST_TYPE_INT

logger = logging.getLogger(__name__)

CHUNK_SIZE = 50_000
_DIGIT_RE  = re.compile(r'\D')


# ── Phone normalization ──────────────────────────────────────────────────────

def _normalize(raw) -> int | None:
    """Return 10-digit number as int, or None if invalid."""
    if raw is None:
        return None
    digits = _DIGIT_RE.sub('', str(raw))
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits[0] in ('0', '1') or digits[3] in ('0', '1'):
        return None
    return int(digits)


# ── Column detection ─────────────────────────────────────────────────────────

def _find_columns(headers: list[str]) -> tuple[int, int | None]:
    """
    Given a list of column headers (lowercased), return
    (phone_col_index, state_col_index_or_None).

    Known master format: fname lname address state city zip phone_number
    """
    headers_lower = [h.lower().strip() for h in headers]

    # Phone: prefer exact 'phone_number', then anything containing 'phone'
    phone_idx = None
    for i, h in enumerate(headers_lower):
        if h == 'phone_number':
            phone_idx = i
            break
    if phone_idx is None:
        for i, h in enumerate(headers_lower):
            if 'phone' in h:
                phone_idx = i
                break
    if phone_idx is None:
        phone_idx = 6  # fallback: 7th column for known master format

    # State: prefer exact 'state', then anything containing 'state'
    state_idx = None
    for i, h in enumerate(headers_lower):
        if h == 'state':
            state_idx = i
            break
    if state_idx is None:
        for i, h in enumerate(headers_lower):
            if 'state' in h:
                state_idx = i
                break
    if state_idx is None:
        state_idx = 3  # fallback: 4th column for known master format

    return phone_idx, state_idx


# ── File readers ─────────────────────────────────────────────────────────────

def _iter_csv(file_obj):
    """
    Yield (phone_col_idx, state_col_idx, row_list) for every data row.
    First yields column indices from the header, then data rows.
    """
    try:
        content = file_obj.read().decode('utf-8')
    except UnicodeDecodeError:
        file_obj.seek(0)
        content = file_obj.read().decode('latin-1')

    content = content.lstrip('﻿')
    reader  = csv.reader(io.StringIO(content))

    headers   = None
    phone_idx = 6
    state_idx = 3

    for row in reader:
        if not any(row):
            continue
        if headers is None:
            headers   = row
            phone_idx, state_idx = _find_columns(headers)
            continue
        yield phone_idx, state_idx, row


def _iter_excel(file_obj):
    """Yield (phone_col_idx, state_col_idx, row_list) for every data row."""
    import openpyxl
    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
    ws = wb.active

    headers   = None
    phone_idx = 6
    state_idx = 3

    for row in ws.iter_rows(values_only=True):
        row = list(row)
        if not any(r for r in row if r is not None):
            continue
        if headers is None:
            headers   = [str(c) if c is not None else '' for c in row]
            phone_idx, state_idx = _find_columns(headers)
            continue
        yield phone_idx, state_idx, row

    wb.close()


# ── PostgreSQL bulk insert ───────────────────────────────────────────────────

def _copy_chunk(cursor, rows: list[tuple], list_type_int: int) -> int:
    """
    Bulk-insert a list of (number_int, state_str_or_None) tuples into
    dnc_master_numbers using PostgreSQL COPY.  Returns rows inserted.
    """
    buf = io.StringIO()
    for number, state in rows:
        state_val = state[:2] if state and str(state).strip() else '\\N'
        buf.write(f"{number}\t{list_type_int}\t{state_val}\n")
    buf.seek(0)
    cursor.copy_from(
        buf,
        'dnc_master_numbers',
        columns=('number', 'list_type', 'state'),
        null='\\N',
        sep='\t',
    )
    return len(rows)


# ── Main Celery task ─────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name='dnc_master.tasks.load_dnc_master_file',
    max_retries=0,
    time_limit=7200,
    soft_time_limit=7000,
)
def load_dnc_master_file(self, upload_job_id: int) -> dict:
    try:
        job = DncUploadJob.objects.get(pk=upload_job_id)
    except DncUploadJob.DoesNotExist:
        logger.error("DncUploadJob %s not found", upload_job_id)
        return {}

    job.status = DncUploadJob.Status.PROCESSING
    job.save(update_fields=['status'])

    list_type_int = LIST_TYPE_INT[job.list_type]

    try:
        # ── Open file ────────────────────────────────────────────────────
        job.file.open('rb')
        filename = job.original_filename.lower()
        is_excel = filename.endswith(('.xlsx', '.xls'))

        if is_excel:
            row_iter = _iter_excel(job.file)
        else:
            row_iter = _iter_csv(job.file)

        with connection.cursor() as cursor:

            # ── Replace mode: delete existing rows for this list type ────
            if job.mode == DncUploadJob.Mode.REPLACE:
                cursor.execute(
                    "DELETE FROM dnc_master_numbers WHERE list_type = %s",
                    [list_type_int],
                )
                logger.info("Deleted existing rows for list_type=%s", list_type_int)

            # ── Stream + bulk insert ─────────────────────────────────────
            chunk     = []
            total_loaded = 0

            for phone_idx, state_idx, row in row_iter:
                try:
                    raw_phone = row[phone_idx] if phone_idx < len(row) else None
                    raw_state = row[state_idx] if state_idx < len(row) else None
                except (IndexError, TypeError):
                    continue

                number = _normalize(raw_phone)
                if number is None:
                    continue

                state = str(raw_state).strip()[:2].upper() if raw_state else None
                chunk.append((number, state))

                if len(chunk) >= CHUNK_SIZE:
                    inserted = _copy_chunk(cursor, chunk, list_type_int)
                    total_loaded += inserted
                    chunk = []
                    job.records_loaded = total_loaded
                    job.save(update_fields=['records_loaded', 'updated_at'])
                    logger.info("Loaded %d rows so far for job %s", total_loaded, upload_job_id)

            # Final partial chunk
            if chunk:
                inserted = _copy_chunk(cursor, chunk, list_type_int)
                total_loaded += inserted

        job.file.close()

        # ── Update DncMasterList stats ───────────────────────────────────
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM dnc_master_numbers WHERE list_type = %s",
                [list_type_int],
            )
            actual_count = cursor.fetchone()[0]

        master_list, _ = DncMasterList.objects.get_or_create(list_type=job.list_type)
        master_list.record_count      = actual_count
        master_list.last_updated      = timezone.now()
        master_list.last_uploaded_by  = job.uploaded_by
        master_list.is_loading        = False
        master_list.save()

        job.records_loaded = total_loaded
        job.status         = DncUploadJob.Status.COMPLETED
        job.save(update_fields=['records_loaded', 'status', 'updated_at'])

        logger.info(
            "DncUploadJob %s COMPLETED — %d rows loaded, %d in DB",
            upload_job_id, total_loaded, actual_count,
        )
        return {'loaded': total_loaded, 'db_count': actual_count}

    except Exception as exc:
        logger.exception("DncUploadJob %s FAILED: %s", upload_job_id, exc)
        try:
            job.file.close()
        except Exception:
            pass
        job.status        = DncUploadJob.Status.FAILED
        job.error_message = f"{type(exc).__name__}: {exc}"
        job.save(update_fields=['status', 'error_message', 'updated_at'])

        master_list, _ = DncMasterList.objects.get_or_create(list_type=job.list_type)
        master_list.is_loading = False
        master_list.save(update_fields=['is_loading'])
        raise
