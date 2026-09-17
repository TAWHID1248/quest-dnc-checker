from dnc_master.models import DncMasterList


def dnc_last_updated(request):
    if not request.user.is_authenticated:
        return {}
    last = (
        DncMasterList.objects
        .exclude(last_updated__isnull=True)
        .order_by('-last_updated')
        .values_list('last_updated', flat=True)
        .first()
    )
    return {'dnc_last_updated': last}


def google_auth(request):
    from django.conf import settings
    return {'google_auth_enabled': bool(settings.GOOGLE_CLIENT_ID)}
