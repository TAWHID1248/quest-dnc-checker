from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.models import CustomUser
from billing.models import CreditTransaction, Payment, PaymentMethod
from scrubber.models import ScrubJob
from support.models import SupportTicket
from .decorators import admin_required


# ── Dashboard ──────────────────────────────────────────────────────────────

@admin_required
def dashboard(request):
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total_clients   = CustomUser.objects.filter(role='client').count()
    active_clients  = CustomUser.objects.filter(role='client', is_active=True).count()
    total_scrubs    = ScrubJob.objects.count()
    open_tickets    = SupportTicket.objects.filter(status__in=['open', 'in_progress', 'waiting']).count()

    monthly_revenue = (
        Payment.objects
        .filter(status='completed', created_at__gte=month_start)
        .aggregate(total=Sum('amount'))['total'] or 0
    )
    total_revenue = (
        Payment.objects
        .filter(status='completed')
        .aggregate(total=Sum('amount'))['total'] or 0
    )

    recent_clients = (
        CustomUser.objects
        .filter(role='client')
        .order_by('-date_joined')[:8]
    )
    recent_tickets = (
        SupportTicket.objects
        .select_related('user')
        .order_by('-created_at')[:8]
    )

    return render(request, 'admin_panel/dashboard.html', {
        'total_clients':   total_clients,
        'active_clients':  active_clients,
        'total_scrubs':    total_scrubs,
        'open_tickets':    open_tickets,
        'monthly_revenue': monthly_revenue,
        'total_revenue':   total_revenue,
        'recent_clients':  recent_clients,
        'recent_tickets':  recent_tickets,
    })


# ── Client Management ──────────────────────────────────────────────────────

@admin_required
def client_list(request):
    q      = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')

    clients = CustomUser.objects.filter(role='client').order_by('-date_joined')

    if q:
        clients = clients.filter(
            Q(email__icontains=q) | Q(name__icontains=q) | Q(company__icontains=q)
        )
    if status == 'active':
        clients = clients.filter(is_active=True)
    elif status == 'inactive':
        clients = clients.filter(is_active=False)

    clients = clients.annotate(
        scrub_count=Count('scrub_jobs', distinct=True),
        payment_count=Count('payments', distinct=True),
    )

    return render(request, 'admin_panel/client_list.html', {
        'clients': clients,
        'q': q,
        'status': status,
        'total': clients.count(),
    })


@admin_required
def client_detail(request, user_id):
    client = get_object_or_404(CustomUser, pk=user_id)

    stats = ScrubJob.objects.filter(user=client).aggregate(
        total_numbers=Sum('total'),
        total_clean=Sum('clean'),
        total_dnc=Sum('dnc'),
    )
    total_spent = Payment.objects.filter(user=client, status='completed').aggregate(s=Sum('amount'))['s'] or 0

    scrub_jobs = ScrubJob.objects.filter(user=client).order_by('-created_at')[:20]
    payments   = Payment.objects.filter(user=client).order_by('-created_at')[:20]
    tickets    = SupportTicket.objects.filter(user=client).order_by('-created_at')[:10]
    txns       = CreditTransaction.objects.filter(user=client).order_by('-created_at')[:20]

    return render(request, 'admin_panel/client_detail.html', {
        'client':      client,
        'scrub_jobs':  scrub_jobs,
        'payments':    payments,
        'tickets':     tickets,
        'txns':        txns,
        'stats':       stats,
        'total_spent': total_spent,
    })


@admin_required
def client_toggle(request, user_id):
    if request.method != 'POST':
        return redirect('admin_panel:client_list')
    client = get_object_or_404(CustomUser, pk=user_id)
    client.is_active = not client.is_active
    client.save(update_fields=['is_active'])
    action = 'activated' if client.is_active else 'deactivated'
    messages.success(request, f'{client.email} has been {action}.')
    next_url = request.POST.get('next', 'admin_panel:client_list')
    return redirect(next_url)


@admin_required
def client_adjust_credits(request, user_id):
    if request.method != 'POST':
        return redirect('admin_panel:client_list')
    client = get_object_or_404(CustomUser, pk=user_id)
    try:
        amount = int(request.POST.get('amount', 0))
    except ValueError:
        messages.error(request, 'Invalid credit amount.')
        return redirect('admin_panel:client_detail', user_id=user_id)

    client.credits += amount
    client.save(update_fields=['credits'])

    CreditTransaction.objects.create(
        user=client,
        type='adjustment',
        amount=amount,
        price=0,
    )
    messages.success(request, f'Adjusted {amount:+} credits for {client.email}. New balance: {client.credits}.')
    next_url = request.POST.get('next', '')
    if next_url == 'list':
        return redirect('admin_panel:client_list')
    return redirect('admin_panel:client_detail', user_id=user_id)


# ── Support Tickets ────────────────────────────────────────────────────────

@admin_required
def ticket_list(request):
    status_filter   = request.GET.get('status', '')
    priority_filter = request.GET.get('priority', '')
    q               = request.GET.get('q', '').strip()

    tickets = SupportTicket.objects.select_related('user').order_by('-created_at')

    if status_filter:
        tickets = tickets.filter(status=status_filter)
    if priority_filter:
        tickets = tickets.filter(priority=priority_filter)
    if q:
        tickets = tickets.filter(
            Q(ticket_id__icontains=q) | Q(subject__icontains=q) | Q(user__email__icontains=q)
        )

    counts = {
        'all':         SupportTicket.objects.count(),
        'open':        SupportTicket.objects.filter(status='open').count(),
        'in_progress': SupportTicket.objects.filter(status='in_progress').count(),
        'waiting':     SupportTicket.objects.filter(status='waiting').count(),
        'resolved':    SupportTicket.objects.filter(status='resolved').count(),
        'closed':      SupportTicket.objects.filter(status='closed').count(),
    }

    return render(request, 'admin_panel/ticket_list.html', {
        'tickets':         tickets,
        'status_filter':   status_filter,
        'priority_filter': priority_filter,
        'q':               q,
        'counts':          counts,
    })


@admin_required
def ticket_update_status(request, ticket_id):
    if request.method != 'POST':
        return redirect('admin_panel:ticket_list')
    ticket = get_object_or_404(SupportTicket, pk=ticket_id)
    new_status = request.POST.get('status')
    valid = [s[0] for s in SupportTicket.Status.choices]
    if new_status in valid:
        ticket.status = new_status
        ticket.save(update_fields=['status', 'updated_at'])
        messages.success(request, f'Ticket {ticket.ticket_id} status updated to {ticket.get_status_display()}.')
    return redirect('admin_panel:ticket_list')


# ── Payment History ────────────────────────────────────────────────────────

@admin_required
def payment_list(request):
    status_filter = request.GET.get('status', '')
    q             = request.GET.get('q', '').strip()

    payments = Payment.objects.select_related('user', 'method').order_by('-created_at')

    if status_filter:
        payments = payments.filter(status=status_filter)
    if q:
        payments = payments.filter(
            Q(payment_id__icontains=q) | Q(user__email__icontains=q) | Q(stripe_pi_id__icontains=q)
        )

    agg = Payment.objects.filter(status='completed').aggregate(
        total_revenue=Sum('amount'),
        total_credits_sold=Sum('credits'),
    )
    payment_counts = {
        'completed': Payment.objects.filter(status='completed').count(),
        'pending':   Payment.objects.filter(status='pending').count(),
        'failed':    Payment.objects.filter(status='failed').count(),
        'refunded':  Payment.objects.filter(status='refunded').count(),
    }

    return render(request, 'admin_panel/payment_list.html', {
        'payments':       payments,
        'status_filter':  status_filter,
        'q':              q,
        'total_revenue':  agg['total_revenue'] or 0,
        'total_credits_sold': agg['total_credits_sold'] or 0,
        'payment_counts': payment_counts,
    })


# ── Scrub Jobs (global) ────────────────────────────────────────────────────

@admin_required
def scrub_job_list(request):
    status_filter = request.GET.get('status', '')
    q             = request.GET.get('q', '').strip()

    jobs = ScrubJob.objects.select_related('user').order_by('-created_at')

    if status_filter:
        jobs = jobs.filter(status=status_filter)
    if q:
        jobs = jobs.filter(
            Q(job_id__icontains=q) | Q(user__email__icontains=q) | Q(filename__icontains=q)
        )

    counts = {s[0]: ScrubJob.objects.filter(status=s[0]).count() for s in ScrubJob.Status.choices}
    counts['all'] = ScrubJob.objects.count()

    agg = ScrubJob.objects.aggregate(
        total_numbers=Sum('total'),
        total_clean=Sum('clean'),
        total_dnc=Sum('dnc'),
    )

    return render(request, 'admin_panel/scrub_job_list.html', {
        'jobs':          jobs,
        'status_filter': status_filter,
        'q':             q,
        'counts':        counts,
        'agg':           agg,
        'status_choices': ScrubJob.Status.choices,
    })


# ── Credit Transactions (global) ───────────────────────────────────────────

@admin_required
def transaction_list(request):
    type_filter = request.GET.get('type', '')
    q           = request.GET.get('q', '').strip()

    txns = CreditTransaction.objects.select_related('user', 'scrub_job').order_by('-created_at')

    if type_filter:
        txns = txns.filter(type=type_filter)
    if q:
        txns = txns.filter(
            Q(transaction_id__icontains=q) | Q(user__email__icontains=q)
        )

    type_counts = {t[0]: CreditTransaction.objects.filter(type=t[0]).count() for t in CreditTransaction.Type.choices}
    type_counts['all'] = CreditTransaction.objects.count()

    agg = CreditTransaction.objects.aggregate(
        total_purchased=Sum('amount', filter=Q(type='purchase')),
        total_used=Sum('amount', filter=Q(type='usage')),
    )

    return render(request, 'admin_panel/transaction_list.html', {
        'txns':          txns,
        'type_filter':   type_filter,
        'q':             q,
        'type_counts':   type_counts,
        'agg':           agg,
        'type_choices':  CreditTransaction.Type.choices,
    })


# ── Payment Methods (global) ───────────────────────────────────────────────

@admin_required
def payment_method_list(request):
    q = request.GET.get('q', '').strip()

    methods = PaymentMethod.objects.select_related('user').order_by('user__email', '-is_default')

    if q:
        methods = methods.filter(
            Q(user__email__icontains=q) | Q(last4__icontains=q) | Q(stripe_pm_id__icontains=q)
        )

    return render(request, 'admin_panel/payment_method_list.html', {
        'methods': methods,
        'q':       q,
        'total':   methods.count(),
    })


# ── Client Create / Edit ───────────────────────────────────────────────────

@admin_required
def client_create(request):
    if request.method == 'POST':
        email    = request.POST.get('email', '').strip().lower()
        name     = request.POST.get('name', '').strip()
        phone    = request.POST.get('phone', '').strip()
        company  = request.POST.get('company', '').strip()
        role     = request.POST.get('role', 'client')
        password = request.POST.get('password', '').strip()

        if not email or not password:
            messages.error(request, 'Email and password are required.')
            return render(request, 'admin_panel/client_form.html', {
                'form_title': 'Create User',
                'post': request.POST,
            })

        if CustomUser.objects.filter(email=email).exists():
            messages.error(request, f'A user with email {email} already exists.')
            return render(request, 'admin_panel/client_form.html', {
                'form_title': 'Create User',
                'post': request.POST,
            })

        if role not in [r[0] for r in CustomUser.Role.choices]:
            role = 'client'

        try:
            user = CustomUser.objects.create_user(
                email=email, password=password,
                name=name, phone=phone, company=company, role=role,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('Failed to create user %s', email)
            messages.error(request, f'Error creating user: {exc}')
            return render(request, 'admin_panel/client_form.html', {
                'form_title': 'Create User',
                'post': request.POST,
            })

        messages.success(request, f'User {user.email} created successfully.')
        return redirect('admin_panel:client_detail', user_id=user.pk)

    return render(request, 'admin_panel/client_form.html', {
        'form_title': 'Create User',
        'post': {},
    })


@admin_required
def client_edit(request, user_id):
    client = get_object_or_404(CustomUser, pk=user_id)

    if request.method == 'POST':
        client.name    = request.POST.get('name', '').strip()
        client.phone   = request.POST.get('phone', '').strip()
        client.company = request.POST.get('company', '').strip()

        role = request.POST.get('role', client.role)
        if role in [r[0] for r in CustomUser.Role.choices]:
            client.role = role

        client.is_active = request.POST.get('is_active') == '1'

        new_password = request.POST.get('password', '').strip()
        if new_password:
            client.set_password(new_password)

        client.save()
        messages.success(request, f'{client.email} updated successfully.')
        return redirect('admin_panel:client_detail', user_id=user_id)

    return render(request, 'admin_panel/client_form.html', {
        'form_title': f'Edit — {client.email}',
        'client': client,
        'post': {},
    })
