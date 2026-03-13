from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from flask import abort, current_app, request


F = TypeVar("F", bound=Callable[..., Any])


def require_admin(f: F) -> F:
    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any):
        token = current_app.config.get("ADMIN_TOKEN")
        if not token:
            abort(503, description="ADMIN_TOKEN is not set")

        provided = request.headers.get("X-Admin-Token") or request.args.get("token")
        if not provided or provided != token:
            abort(403)

        return f(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
