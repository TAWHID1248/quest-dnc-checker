import logging

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from admin_panel.decorators import admin_required
from .models import DncMasterList, DncUploadJob, LIST_TYPES

logger = logging.getLogger(__name__)

_ALLOWED_EXTENSIONS = {'.csv', '.txt', '.xlsx', '.xls'}


@admin_required
def dnc_master_home(request):
    master_lists = {m.list_type: m for m in DncMasterList.objects.all()}
    upload_history = DncUploadJob.objects.select_related('uploaded_by').order_by('-created_at')[:30]

    list_stats = []
    for key, label in LIST_TYPES:
        ml = master_lists.get(key)
        list_stats.append({
            'key':          key,
            'label':        label,
            'record_count': ml.record_count if ml else 0,
            'last_updated': ml.last_updated if ml else None,
            'uploaded_by':  ml.last_uploaded_by if ml else None,
            'is_loading':   ml.is_loading if ml else False,
        })

    return render(request, 'admin_panel/dnc_master.html', {
        'list_stats':     list_stats,
        'upload_history': upload_history,
        'list_types':     LIST_TYPES,
    })


@admin_required
@require_POST
def dnc_master_upload(request):
    uploaded = request.FILES.get('file')
    list_type = request.POST.get('list_type', '')
    mode      = request.POST.get('mode', 'replace')

    def _err(msg):
        from django.contrib import messages
        messages.error(request, msg)
        return redirect('admin_panel:dnc_master')

    valid_types = [k for k, _ in LIST_TYPES]
    if list_type not in valid_types:
        return _err('Invalid list type selected.')

    if not uploaded:
        return _err('Please choose a file to upload.')

    ext = ''
    if '.' in uploaded.name:
        ext = '.' + uploaded.name.rsplit('.', 1)[-1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return _err('Only CSV, TXT, XLSX, or XLS files are accepted.')

    if mode not in ('replace', 'append'):
        mode = 'replace'

    # Mark the list as loading
    master_list, _ = DncMasterList.objects.get_or_create(list_type=list_type)
    master_list.is_loading = True
    master_list.save(update_fields=['is_loading'])

    job = DncUploadJob.objects.create(
        list_type=list_type,
        mode=mode,
        file=uploaded,
        original_filename=uploaded.name,
        uploaded_by=request.user,
        status=DncUploadJob.Status.PENDING,
    )
    logger.info(
        "DncUploadJob %s created by %s — list=%s mode=%s file=%s",
        job.pk, request.user.email, list_type, mode, uploaded.name,
    )

    try:
        from .tasks import load_dnc_master_file
        load_dnc_master_file.delay(job.pk)
    except Exception as exc:
        logger.warning("Celery unavailable (%s) — running upload in thread", exc)
        import threading
        t = threading.Thread(
            target=load_dnc_master_file.run if hasattr(load_dnc_master_file, 'run')
                   else __import__('dnc_master.tasks', fromlist=['load_dnc_master_file']).load_dnc_master_file,
            args=(job.pk,),
            daemon=True,
        )
        t.start()

    from django.contrib import messages
    messages.success(
        request,
        f'Upload started for {job.get_list_type_display()} ({mode} mode). '
        f'Processing in background — refresh to see progress.',
    )
    return redirect('admin_panel:dnc_master')


@admin_required
def dnc_master_upload_status(request, job_id):
    """JSON endpoint polled by the frontend for upload progress."""
    try:
        job = DncUploadJob.objects.get(pk=job_id)
    except DncUploadJob.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    return JsonResponse({
        'job_id':         job.pk,
        'status':         job.status,
        'total_rows':     job.total_rows,
        'records_loaded': job.records_loaded,
        'progress_pct':   job.progress_pct,
        'error_message':  job.error_message,
    })
