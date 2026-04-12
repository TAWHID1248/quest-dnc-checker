import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .models import ScrubJob

logger = logging.getLogger(__name__)

_VALID_SCRUB_TYPES = {'federal_dnc', 'state_dnc', 'litigator'}
_ALLOWED_EXTENSIONS = {'.csv', '.txt'}
_MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 MB hard cap


@login_required
def scrubber_home(request):
    recent_jobs = ScrubJob.objects.filter(user=request.user).order_by('-created_at')[:20]

    if request.method == 'POST':
        return _handle_upload(request, recent_jobs)

    return render(request, 'scrubber/home.html', {'recent_jobs': recent_jobs})


def _handle_upload(request, recent_jobs):
    """Validate the upload, create a ScrubJob, and dispatch the Celery task."""
    # ── Validate scrub types ─────────────────────────────────────────────
    selected_types = request.POST.getlist('scrub_types')
    selected_types = [t for t in selected_types if t in _VALID_SCRUB_TYPES]

    if not selected_types:
        messages.error(request, 'Please select at least one scrub type.')
        return render(request, 'scrubber/home.html', {'recent_jobs': recent_jobs})

    # ── Validate file ────────────────────────────────────────────────────
    uploaded = request.FILES.get('file')

    if not uploaded:
        messages.error(request, 'Please upload a file.')
        return render(request, 'scrubber/home.html', {'recent_jobs': recent_jobs})

    ext = '.' + uploaded.name.rsplit('.', 1)[-1].lower() if '.' in uploaded.name else ''
    if ext not in _ALLOWED_EXTENSIONS:
        messages.error(request, 'Only .csv and .txt files are accepted.')
        return render(request, 'scrubber/home.html', {'recent_jobs': recent_jobs})

    if uploaded.size > _MAX_FILE_SIZE:
        messages.error(request, f'File too large. Maximum size is {_MAX_FILE_SIZE // 1024 // 1024} MB.')
        return render(request, 'scrubber/home.html', {'recent_jobs': recent_jobs})

    # ── Credit quick-check (non-atomic, just for fast feedback) ──────────
    # The task performs an atomic check with SELECT FOR UPDATE before
    # actually deducting — this check is just to avoid queuing obviously
    # doomed jobs.
    if request.user.credits <= 0:
        messages.error(
            request,
            'You have no credits remaining. '
            '<a href="/billing/" class="alert-link">Buy credits</a> to continue.',
        )
        return render(request, 'scrubber/home.html', {'recent_jobs': recent_jobs})

    # ── Create job record ────────────────────────────────────────────────
    job = ScrubJob.objects.create(
        user=request.user,
        filename=uploaded.name,
        file=uploaded,
        scrub_types=selected_types,
        status=ScrubJob.Status.QUEUED,
    )
    logger.info(
        "Created ScrubJob %s for user %s (file=%s, types=%s)",
        job.job_id, request.user.email, uploaded.name, selected_types,
    )

    # ── Dispatch Celery task ─────────────────────────────────────────────
    try:
        from .tasks import process_scrub_job
        process_scrub_job.delay(job.pk)
        messages.success(
            request,
            f'Job <strong>{job.job_id}</strong> has been queued. '
            'Results will appear below once processing is complete.',
        )
    except Exception as exc:
        # Celery broker might be unavailable — mark the job failed immediately
        logger.exception("Failed to enqueue ScrubJob %s: %s", job.job_id, exc)
        job.status = ScrubJob.Status.FAILED
        job.error_message = f'Could not queue task: {exc}'
        job.save(update_fields=['status', 'error_message'])
        messages.error(
            request,
            'The processing queue is currently unavailable. '
            'Please try again in a few minutes.',
        )

    return redirect('scrubber:home')
