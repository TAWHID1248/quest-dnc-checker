from functools import wraps
from django.shortcuts import redirect


def admin_required(view_func):
    """Allow access only to authenticated users with role=admin."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/accounts/login/?next={request.path}')
        if not request.user.is_admin:
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper
