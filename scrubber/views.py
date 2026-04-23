import logging
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .models import ScrubJob

logger = logging.getLogger(__name__)

_VALID_SCRUB_TYPES = {'federal_dnc', 'state_dnc'}
_ALLOWED_EXTENSIONS = {'.csv', '.txt'}
_MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 MB hard cap


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
        'job_id': job.job_id,
        'status': job.status,
        'status_display': job.get_status_display(),
        'total': job.total,
        'clean': job.clean,
        'dnc': job.dnc,
        'state_dnc': job.state_dnc,
        'error_message': job.error_message,
        'result_url': reverse('scrubber:download_result', args=[job.job_id]) if job.result_file else None,
        'result_url_dnc': reverse('scrubber:download_result_dnc', args=[job.job_id]) if job.result_file_dnc else None,
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


def _handle_upload(request, recent_jobs):
    """Validate the upload, create a ScrubJob, and dispatch the Celery task."""
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

    # ── Validate file ────────────────────────────────────────────────────
    uploaded = request.FILES.get('file')

    if not uploaded:
        return _error('Please upload a file.')

    ext = '.' + uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else ''
    if ext not in _ALLOWED_EXTENSIONS:
        return _error('Only .csv and .txt files are accepted.')

    if uploaded.size > _MAX_FILE_SIZE:
        return _error(f'File too large. Maximum size is {_MAX_FILE_SIZE // 1024 // 1024} MB.')

    # ── Credit quick-check (non-atomic, just for fast feedback) ──────────
    # The task performs an atomic check with SELECT FOR UPDATE before
    # actually deducting — this check is just to avoid queuing obviously
    # doomed jobs.
    if request.user.credits <= 0:
        return _error(
            'You have no credits remaining. '
            '<a href="/billing/" class="alert-link">Buy credits</a> to continue.'
        )

    # ── Create job record ────────────────────────────────────────────────
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
