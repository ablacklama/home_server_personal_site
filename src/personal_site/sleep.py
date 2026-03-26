from __future__ import annotations

import datetime as dt
import json

from flask import (
    Blueprint,
    current_app,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import select

from .activity_log import log_activity
from .sleep_models import SleepEntry

bp = Blueprint("sleep", __name__, url_prefix="/sleep")


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
        select(SleepEntry).order_by(SleepEntry.slept_on.desc())
    ).all()
    recent: list[dict] = []
    for entry in entries[:25]:
        hours = entry.duration_minutes // 60
        minutes = entry.duration_minutes % 60
        recent.append(
            {
                "entry_id": entry.id,
                "slept_on": entry.slept_on.isoformat(),
                "duration_hours": hours,
                "duration_minutes": minutes,
                "quality": entry.quality,
                "notes": entry.notes,
            }
        )

    return {
        "recent_sleep": recent,
        "default_slept_on": dt.date.today().isoformat(),
        "error": error,
    }


def _render_index(message: str | None = None):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "sleep.html",
            title="Sleep",
            recent_sleep=[],
            default_slept_on=dt.date.today().isoformat(),
            error=message
            or current_app.config.get("DB_ERROR")
            or "DATABASE_URL is not set (configure SQLite to use sleep tracking)",
        )

    with SessionLocal() as session:
        ctx = _load_index_context(session, error=message)
    return render_template("sleep.html", title="Sleep", **ctx)


def _render_entry_response(
    session, message: str | None = None, status: int = 200, trigger: dict | None = None
):
    ctx = _load_index_context(session, error=message)
    ctx["oob"] = True
    return _make_htmx_response(
        "partials/sleep_entry_response.html", ctx, status=status, trigger=trigger
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
                "partials/sleep_entry_response.html",
                {"recent_sleep": [], "error": message, "oob": True},
                status=503,
            )
        return _render_index(message), 503

    with SessionLocal() as session:
        slept_on_raw = (request.form.get("slept_on") or "").strip()
        duration_hours_raw = (request.form.get("duration_hours") or "").strip()
        duration_minutes_raw = (request.form.get("duration_minutes") or "").strip()
        quality_raw = (request.form.get("quality") or "").strip()
        notes = (request.form.get("notes") or "").strip() or None

        if slept_on_raw:
            try:
                slept_on = dt.date.fromisoformat(slept_on_raw)
            except ValueError:
                if _is_htmx():
                    return _render_entry_response(
                        session, "slept_on must be a date", status=400
                    )
                return _render_index("slept_on must be a date"), 400
        else:
            slept_on = dt.date.today()

        try:
            hours = int(duration_hours_raw or 0)
            minutes = int(duration_minutes_raw or 0)
        except ValueError:
            message = "duration must be numeric"
            if _is_htmx():
                return _render_entry_response(session, message, status=400)
            return _render_index(message), 400
        if hours < 0 or minutes < 0 or minutes > 59:
            message = "duration minutes must be 0-59 and hours >= 0"
            if _is_htmx():
                return _render_entry_response(session, message, status=400)
            return _render_index(message), 400

        duration_total = hours * 60 + minutes
        if duration_total <= 0:
            message = "sleep duration is required"
            if _is_htmx():
                return _render_entry_response(session, message, status=400)
            return _render_index(message), 400

        quality = None
        if quality_raw:
            try:
                quality = int(quality_raw)
            except ValueError:
                message = "quality must be an integer"
                if _is_htmx():
                    return _render_entry_response(session, message, status=400)
                return _render_index(message), 400
            if quality < 1 or quality > 5:
                message = "quality must be between 1 and 5"
                if _is_htmx():
                    return _render_entry_response(session, message, status=400)
                return _render_index(message), 400

        session.add(
            SleepEntry(
                slept_on=slept_on,
                duration_minutes=duration_total,
                quality=quality,
                notes=notes,
            )
        )
        session.commit()

        log_activity("sleep", "create", f"{slept_on} {duration_total}min q={quality}")

        if _is_htmx():
            return _render_entry_response(session, trigger={"sleepEntrySaved": True})
        return _render_index()


@bp.get("/entries/<entry_id>/edit")
def edit_entry_form(entry_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_index("Database is not configured"), 503

    with SessionLocal() as session:
        entry = session.get(SleepEntry, entry_id)
        if entry is None:
            return _render_index("Sleep entry not found"), 404

        return render_template(
            "sleep_edit.html",
            title="Edit Sleep",
            entry=entry,
            duration_hours=entry.duration_minutes // 60,
            duration_minutes=entry.duration_minutes % 60,
        )


@bp.post("/entries/<entry_id>/edit")
def edit_entry(entry_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_index("Database is not configured"), 503

    with SessionLocal() as session:
        entry = session.get(SleepEntry, entry_id)
        if entry is None:
            if _is_htmx():
                return _render_entry_response(
                    session, "Sleep entry not found", status=404
                )
            return _render_index("Sleep entry not found"), 404

        slept_on_raw = (request.form.get("slept_on") or "").strip()
        duration_hours_raw = (request.form.get("duration_hours") or "").strip()
        duration_minutes_raw = (request.form.get("duration_minutes") or "").strip()
        quality_raw = (request.form.get("quality") or "").strip()
        notes = (request.form.get("notes") or "").strip() or None

        if slept_on_raw:
            try:
                slept_on = dt.date.fromisoformat(slept_on_raw)
            except ValueError:
                return _render_index("slept_on must be a date"), 400
        else:
            slept_on = entry.slept_on

        try:
            hours = int(duration_hours_raw or 0)
            minutes = int(duration_minutes_raw or 0)
        except ValueError:
            return _render_index("duration must be numeric"), 400
        if hours < 0 or minutes < 0 or minutes > 59:
            return _render_index("duration minutes must be 0-59 and hours >= 0"), 400

        duration_total = hours * 60 + minutes
        if duration_total <= 0:
            return _render_index("sleep duration is required"), 400

        quality = None
        if quality_raw:
            try:
                quality = int(quality_raw)
            except ValueError:
                return _render_index("quality must be an integer"), 400
            if quality < 1 or quality > 5:
                return _render_index("quality must be between 1 and 5"), 400

        entry.slept_on = slept_on
        entry.duration_minutes = duration_total
        entry.quality = quality
        entry.notes = notes
        session.commit()

    log_activity("sleep", "edit", f"id={entry_id} {slept_on} {duration_total}min")
    return redirect(url_for("sleep.index"))


@bp.post("/entries/<entry_id>/delete")
def delete_entry(entry_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured (set DATABASE_URL)"
        if _is_htmx():
            return _make_htmx_response(
                "partials/sleep_entry_response.html",
                {"recent_sleep": [], "error": message, "oob": True},
                status=503,
            )
        return _render_index(message), 503

    with SessionLocal() as session:
        entry = session.get(SleepEntry, entry_id)
        if entry is None:
            if _is_htmx():
                return _render_entry_response(
                    session, "Sleep entry not found", status=404
                )
            return _render_index("Sleep entry not found"), 404

        session.delete(entry)
        session.commit()

        log_activity("sleep", "delete", f"id={entry_id}")

        if _is_htmx():
            return _render_entry_response(session)

    return _render_index()
