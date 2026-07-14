import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import render, redirect

from .tasks import send_support_email

logger = logging.getLogger(__name__)


@login_required
def support_home(request):
    form_data = {
        'name': request.user.display_name or '',
        'email': request.user.email or '',
        'number': request.user.phone or '',
        'message': '',
    }

    if request.method == 'POST':
        form_data = {
            'name': request.POST.get('name', '').strip(),
            'email': request.POST.get('email', '').strip(),
            'number': request.POST.get('number', '').strip(),
            'message': request.POST.get('message', '').strip(),
        }

        errors = []
        if not form_data['name']:
            errors.append('Please enter your name.')
        if not form_data['email']:
            errors.append('Please enter your email address.')
        else:
            try:
                validate_email(form_data['email'])
            except ValidationError:
                errors.append('Please enter a valid email address.')
        if not form_data['message']:
            errors.append('Please enter a message.')

        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            try:
                send_support_email.delay(
                    form_data['name'], form_data['email'],
                    form_data['number'], form_data['message'],
                )
            except Exception:
                # Broker unavailable — send synchronously so the message isn't lost
                logger.warning("Celery unavailable, sending support email synchronously")
                try:
                    send_support_email(
                        form_data['name'], form_data['email'],
                        form_data['number'], form_data['message'],
                    )
                except Exception:
                    logger.exception("Failed to send support email")
                    messages.error(
                        request,
                        'Sorry, we could not send your message right now. '
                        'Please email us directly at support@checkdnc.net.',
                    )
                    return render(request, 'support/home.html', {'form_data': form_data})
            messages.success(request, "Your message has been sent. We'll get back to you soon!")
            return redirect('support:home')

    return render(request, 'support/home.html', {'form_data': form_data})
