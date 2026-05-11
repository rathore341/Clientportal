from functools import wraps

from flask import flash, redirect, request, session, url_for


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_id"):
            flash("Please login as admin to continue.", "warning")
            return redirect(url_for("admin.login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped_view


def is_safe_next_url(target):
    return bool(target) and target.startswith("/") and not target.startswith("//")
