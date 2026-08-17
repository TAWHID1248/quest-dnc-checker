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


def staff_required(view_func):
    """Allow access to admins and sub-admins (role=admin or role=sub_admin)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/accounts/login/?next={request.path}')
        if not request.user.is_staff_member:
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper
