import logging
import os
import re
from datetime import datetime
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.cache import cache
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import ScrubJob

logger = logging.getLogger(__name__)

_VALID_SCRUB_TYPES = {'federal_dnc', 'state_dnc'}
_ALLOWED_EXTENSIONS = {'.csv', '.txt'}
_MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB hard cap


@login_required
def scrubber_home(request):
    recent_jobs = ScrubJob.objects.filter(user=request.user).order_by('-created_at')[:20]

    if request.method == 'POST':
        return _handle_upload(request, recent_jobs)

    return render(request, 'scrubber/home.html', {'recent_jobs': recent_jobs})


@login_required
def job_status(request, job_id):
    """Return JSON status for a scrub job (used by real-time polling)."""
    job = get_object_or_404(ScrubJob, job_id=job_id, user=request.user)
    return JsonResponse({
        'job_id':          job.job_id,
        'status':          job.status,
        'status_display':  job.get_status_display(),
        'total':           job.total,
        'clean':           job.clean,
        'dnc':             job.dnc,
        'state_dnc':       job.state_dnc,
        'processed_count': job.processed_count,
        'error_message':   job.error_message,
        'result_url':      reverse('scrubber:download_result', args=[job.job_id]) if job.result_file else None,
        'result_url_dnc':  reverse('scrubber:download_result_dnc', args=[job.job_id]) if job.result_file_dnc else None,
        'duration':        job.duration,
    })


@login_required
def download_result(request, job_id):
    """Stream the clean-numbers CSV to the browser. Enforces ownership."""
    job = get_object_or_404(ScrubJob, job_id=job_id, user=request.user)
    if not job.result_file or job.status != ScrubJob.Status.COMPLETED:
        raise Http404
    try:
        f = job.result_file.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    filename = os.path.basename(job.result_file.name)
    return FileResponse(f, as_attachment=True, filename=filename)


@login_required
def download_result_dnc(request, job_id):
    """Stream the DNC-numbers CSV to the browser. Enforces ownership."""
    job = get_object_or_404(ScrubJob, job_id=job_id, user=request.user)
    if not job.result_file_dnc or job.status != ScrubJob.Status.COMPLETED:
        raise Http404
    try:
        f = job.result_file_dnc.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    filename = os.path.basename(job.result_file_dnc.name)
    return FileResponse(f, as_attachment=True, filename=filename)


_CTRL_ACTIVE    = {ScrubJob.Status.QUEUED, ScrubJob.Status.PROCESSING}
_CTRL_PAUSEABLE = {ScrubJob.Status.PROCESSING}
_CTRL_RESUMABLE = {ScrubJob.Status.PAUSED}
_CTRL_CANCELABLE = {
    ScrubJob.Status.QUEUED, ScrubJob.Status.PROCESSING, ScrubJob.Status.PAUSED,
}


@login_required
@require_POST
def job_control(request, job_id):
    """Pause, resume, or cancel a running/paused scrub job."""
    job = get_object_or_404(ScrubJob, job_id=job_id, user=request.user)
    action = request.POST.get('action', '')

    def _err(msg, status=400):
        return JsonResponse({'ok': False, 'error': msg}, status=status)

    if action == 'pause':
        if job.status not in _CTRL_PAUSEABLE:
            return _err('Job is not processing.')
        try:
            cache.set(f'scrub_ctrl_{job.pk}', 'pause', timeout=3600)
        except Exception:
            return _err('Could not send pause signal — please try again.', status=503)
        return JsonResponse({'ok': True, 'action': 'pause'})

    if action == 'resume':
        if job.status not in _CTRL_RESUMABLE:
            return _err('Job is not paused.')
        try:
            cache.delete(f'scrub_ctrl_{job.pk}')
        except Exception:
            pass  # stale key is harmless; worker checks DB status
        from .tasks import process_scrub_job
        process_scrub_job.delay(job.pk)
        return JsonResponse({'ok': True, 'action': 'resume'})

    if action == 'cancel':
        if job.status not in _CTRL_CANCELABLE:
            return _err('Job cannot be cancelled in its current state.')
        if job.status == ScrubJob.Status.PAUSED:
            # Already stopped — cancel immediately without waiting for a task
            from .tasks import _delete_partial
            _delete_partial(job)
            job.status = ScrubJob.Status.CANCELLED
            job.save(update_fields=['status'])
        else:
            try:
                cache.set(f'scrub_ctrl_{job.pk}', 'cancel', timeout=3600)
            except Exception:
                return _err('Could not send cancel signal — please try again.', status=503)
        return JsonResponse({'ok': True, 'action': 'cancel'})

    return _err('Unknown action.')


@login_required
def presign_upload(request):
    """
    Return a presigned S3 PUT URL so the browser can upload large files
    directly to S3, bypassing Railway's proxy size/timeout limits.

    Returns 501 if S3 storage is not configured (local dev falls back to
    the regular direct upload path automatically).

    The browser sends: PUT {put_url} with raw file bytes.
    On success (HTTP 200), the browser calls the normal /scrubber/ POST
    with {file_key, original_filename} instead of an actual file upload.
    """
    bucket = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '')
    if not bucket:
        return JsonResponse({'ok': False, 'error': 'S3 not configured'}, status=501)

    raw_name = request.GET.get('filename', 'upload')
    safe_name = re.sub(r'[^\w.\-]', '_', raw_name)[:200]
    ext = ('.' + safe_name.rsplit('.', 1)[-1].lower()) if '.' in safe_name else ''
    if ext not in _ALLOWED_EXTENSIONS:
        return JsonResponse({'ok': False, 'error': 'Only .csv and .txt files are accepted.'}, status=400)

    date_path = datetime.now().strftime('%Y/%m')
    key = f"scrub_uploads/{date_path}/{uuid4().hex}_{safe_name}"

    try:
        import boto3
        s3 = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=getattr(settings, 'AWS_S3_REGION_NAME', 'us-east-1'),
            endpoint_url=getattr(settings, 'AWS_S3_ENDPOINT_URL', None),
        )
        put_url = s3.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': bucket,
                'Key': key,
                'ContentType': 'application/octet-stream',
            },
            ExpiresIn=7200,  # 2 hours — plenty for slow uploads
        )
    except Exception as exc:
        logger.exception("Failed to generate presigned URL: %s", exc)
        return JsonResponse({'ok': False, 'error': 'Could not generate upload URL.'}, status=500)

    return JsonResponse({'ok': True, 'put_url': put_url, 'key': key})


def _handle_upload(request, recent_jobs):
    """Validate the upload, create a ScrubJob, and dispatch the Celery task.

    Supports two upload modes:
    1. Direct multipart upload  — `request.FILES['file']` present (small files / local dev)
    2. Post-S3-presign notify   — `request.POST['file_key']` present (large files via S3)
    """
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def _error(msg, status=400):
        if is_ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=status)
        messages.error(request, msg)
        return render(request, 'scrubber/home.html', {'recent_jobs': recent_jobs})

    # ── Validate scrub types ─────────────────────────────────────────────
    selected_types = request.POST.getlist('scrub_types')
    selected_types = [t for t in selected_types if t in _VALID_SCRUB_TYPES]

    if not selected_types:
        return _error('Please select at least one scrub type.')

    # ── Credit quick-check (non-atomic, just for fast feedback) ──────────
    if request.user.credits <= 0:
        return _error(
            'You have no credits remaining. '
            '<a href="/billing/" class="alert-link">Buy credits</a> to continue.'
        )

    # ── Determine upload mode ────────────────────────────────────────────
    file_key = request.POST.get('file_key', '').strip()
    original_filename = request.POST.get('original_filename', '').strip()

    if file_key:
        # ── Mode 2: file already on S3 via presigned PUT ─────────────────
        # Validate key prefix to prevent SSRF/path-traversal.
        if not re.match(r'^scrub_uploads/\d{4}/\d{2}/[a-f0-9]{32}_[\w.\-]{1,200}$', file_key):
            return _error('Invalid file key.')
        ext = ('.' + file_key.rsplit('.', 1)[-1].lower()) if '.' in file_key else ''
        if ext not in _ALLOWED_EXTENSIONS:
            return _error('Only .csv and .txt files are accepted.')
        if not original_filename:
            original_filename = file_key.rsplit('_', 1)[-1]

        try:
            job = ScrubJob(
                user=request.user,
                filename=original_filename,
                scrub_types=selected_types,
                status=ScrubJob.Status.QUEUED,
            )
            job.file.name = file_key  # point to already-uploaded S3 object
            job.save()
        except Exception as exc:
            logger.exception("Failed to create ScrubJob (S3 key) for user %s: %s", request.user.email, exc)
            return _error(f'Could not create job: {exc}', status=500)
        logger.info(
            "Created ScrubJob %s for user %s (s3_key=%s, types=%s)",
            job.job_id, request.user.email, file_key, selected_types,
        )

    else:
        # ── Mode 1: direct multipart upload ─────────────────────────────
        uploaded = request.FILES.get('file')

        if not uploaded:
            return _error('Please upload a file.')

        ext = '.' + uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else ''
        if ext not in _ALLOWED_EXTENSIONS:
            return _error('Only .csv and .txt files are accepted.')

        if uploaded.size > _MAX_FILE_SIZE:
            return _error(f'File too large. Maximum size is {_MAX_FILE_SIZE // 1024 // 1024} MB.')

        try:
            job = ScrubJob.objects.create(
                user=request.user,
                filename=uploaded.name,
                file=uploaded,
                scrub_types=selected_types,
                status=ScrubJob.Status.QUEUED,
            )
        except Exception as exc:
            logger.exception("Failed to create ScrubJob for user %s: %s", request.user.email, exc)
            return _error(f'File storage error: {exc}', status=500)
        logger.info(
            "Created ScrubJob %s for user %s (file=%s, types=%s)",
            job.job_id, request.user.email, uploaded.name, selected_types,
        )

    # ── Dispatch Celery task (with thread fallback if broker is down) ────
    try:
        from .tasks import process_scrub_job
        process_scrub_job.delay(job.pk)
        logger.info("ScrubJob %s dispatched to Celery", job.job_id)
    except Exception as celery_exc:
        # Celery broker unavailable — run synchronously in a daemon thread so
        # the HTTP response returns immediately and the frontend can poll.
        logger.warning(
            "Celery unavailable (%s) — running ScrubJob %s in-process thread",
            celery_exc, job.job_id,
        )
        import threading
        from .tasks import run_scrub_job

        t = threading.Thread(target=run_scrub_job, args=(job.pk,), daemon=True)
        t.start()

    if is_ajax:
        return JsonResponse({
            'ok': True,
            'job_id': job.job_id,
            'job_pk': job.pk,
            'filename': job.filename,
            'scrub_types': job.scrub_types,
            'message': f'Job {job.job_id} has been queued.',
        })
    messages.success(
        request,
        f'Job <strong>{job.job_id}</strong> has been queued. '
        'Results will appear below once processing is complete.',
    )
    return redirect('scrubber:home')
