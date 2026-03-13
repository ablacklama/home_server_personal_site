from __future__ import annotations

import datetime as dt
import json

from flask import Blueprint, current_app, make_response, render_template, request
from sqlalchemy import select

from .caffeine_models import CaffeineEntry
from .workouts import ALLOWED_TIME_BUCKETS, _bucket_to_time, _current_time_bucket

bp = Blueprint("caffeine", __name__, url_prefix="/caffeine")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _make_htmx_response(
    template_name: str, context: dict, status: int = 200, trigger: dict | None = None
):
    response = make_response(render_template(template_name, **context), status)
    if trigger:
        response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


def _load_index_context(session, error: str | None = None):
    entries = session.scalars(
        select(CaffeineEntry).order_by(CaffeineEntry.consumed_at.desc())
    ).all()
    recent: list[dict] = []
    for entry in entries[:25]:
        recent.append(
            {
                "entry_id": entry.id,
                "consumed_on": entry.consumed_on.isoformat(),
                "time_bucket": entry.time_bucket,
                "amount_mg": entry.amount_mg,
                "source": entry.source,
                "notes": entry.notes,
            }
        )

    now = dt.datetime.now()
    return {
        "recent_caffeine": recent,
        "default_consumed_on": dt.date.today().isoformat(),
        "default_time_bucket": _current_time_bucket(now),
        "error": error,
    }


def _render_index(message: str | None = None):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "caffeine.html",
            title="Caffeine",
            recent_caffeine=[],
            default_consumed_on=dt.date.today().isoformat(),
            default_time_bucket=_current_time_bucket(dt.datetime.now()),
            error=message
            or current_app.config.get("DB_ERROR")
            or "DATABASE_URL is not set (configure SQLite to use caffeine tracking)",
        )

    with SessionLocal() as session:
        ctx = _load_index_context(session, error=message)
    return render_template("caffeine.html", title="Caffeine", **ctx)


def _render_entry_response(
    session, message: str | None = None, status: int = 200, trigger: dict | None = None
):
    ctx = _load_index_context(session, error=message)
    ctx["oob"] = True
    return _make_htmx_response(
        "partials/caffeine_entry_response.html", ctx, status=status, trigger=trigger
    )


@bp.get("/")
def index():
    return _render_index()


@bp.post("/entries")
def create_entry():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured (set DATABASE_URL)"
        if _is_htmx():
            return _make_htmx_response(
                "partials/caffeine_entry_response.html",
                {"recent_caffeine": [], "error": message, "oob": True},
                status=503,
            )
        return _render_index(message), 503

    with SessionLocal() as session:
        consumed_on_raw = (request.form.get("consumed_on") or "").strip()
        time_bucket = (request.form.get("time_bucket") or "").strip().lower()
        amount_raw = (request.form.get("amount_mg") or "").strip()
        source = (request.form.get("source") or "").strip() or None
        notes = (request.form.get("notes") or "").strip() or None

        if not amount_raw:
            message = "amount_mg is required"
            if _is_htmx():
                return _render_entry_response(session, message, status=400)
            return _render_index(message), 400

        try:
            amount_mg = int(amount_raw)
        except ValueError:
            message = "amount_mg must be an integer"
            if _is_htmx():
                return _render_entry_response(session, message, status=400)
            return _render_index(message), 400
        if amount_mg <= 0:
            message = "amount_mg must be greater than 0"
            if _is_htmx():
                return _render_entry_response(session, message, status=400)
            return _render_index(message), 400

        if consumed_on_raw:
            try:
                consumed_on = dt.date.fromisoformat(consumed_on_raw)
            except ValueError:
                message = "consumed_on must be a date"
                if _is_htmx():
                    return _render_entry_response(session, message, status=400)
                return _render_index(message), 400
        else:
            consumed_on = dt.date.today()

        if not time_bucket:
            time_bucket = _current_time_bucket(dt.datetime.now())
        if time_bucket not in ALLOWED_TIME_BUCKETS:
            message = "time bucket must be morning, afternoon, or night"
            if _is_htmx():
                return _render_entry_response(session, message, status=400)
            return _render_index(message), 400

        consumed_at = dt.datetime.combine(
            consumed_on, _bucket_to_time(time_bucket)
        ).replace(tzinfo=dt.timezone.utc)

        session.add(
            CaffeineEntry(
                consumed_at=consumed_at,
                consumed_on=consumed_on,
                time_bucket=time_bucket,
                amount_mg=amount_mg,
                source=source,
                notes=notes,
            )
        )
        session.commit()

        if _is_htmx():
            return _render_entry_response(session, trigger={"caffeineEntrySaved": True})
        return _render_index()


@bp.post("/entries/<entry_id>/delete")
def delete_entry(entry_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured (set DATABASE_URL)"
        if _is_htmx():
            return _make_htmx_response(
                "partials/caffeine_entry_response.html",
                {"recent_caffeine": [], "error": message, "oob": True},
                status=503,
            )
        return _render_index(message), 503

    with SessionLocal() as session:
        entry = session.get(CaffeineEntry, entry_id)
        if entry is None:
            if _is_htmx():
                return _render_entry_response(
                    session, "Caffeine entry not found", status=404
                )
            return _render_index("Caffeine entry not found"), 404

        session.delete(entry)
        session.commit()

        if _is_htmx():
            return _render_entry_response(session)

    return _render_index()
