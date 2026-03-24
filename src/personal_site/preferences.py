from __future__ import annotations

import json

from flask import Blueprint, current_app, make_response, render_template, request
from sqlalchemy import select

from .preferences_models import UserPreferences
from .reports import generate_weekly_report

bp = Blueprint("preferences", __name__, url_prefix="/preferences")

DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

SECTIONS = ["workouts", "sleep", "nutrition"]


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _get_or_create_prefs(session) -> UserPreferences:
    prefs = session.scalars(select(UserPreferences).limit(1)).first()
    if prefs is None:
        prefs = UserPreferences(report_include=json.dumps(SECTIONS))
        session.add(prefs)
        session.flush()
    return prefs


@bp.get("/")
def index():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "preferences.html",
            title="Preferences",
            prefs=None,
            day_names=DAY_NAMES,
            sections=SECTIONS,
            error="Database not configured.",
        )

    with SessionLocal() as session:
        prefs = _get_or_create_prefs(session)
        prefs_data = {
            "email": prefs.email,
            "report_enabled": prefs.report_enabled,
            "report_day": prefs.report_day,
            "report_hour": prefs.report_hour,
        }
        included = json.loads(prefs.report_include or "[]")
        session.commit()

    return render_template(
        "preferences.html",
        title="Preferences",
        prefs=prefs_data,
        included=included,
        day_names=DAY_NAMES,
        sections=SECTIONS,
        error=None,
    )


@bp.post("/")
def save():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "preferences.html",
            title="Preferences",
            prefs=None,
            day_names=DAY_NAMES,
            sections=SECTIONS,
            error="Database not configured.",
        ), 503

    with SessionLocal() as session:
        prefs = _get_or_create_prefs(session)

        prefs.email = (request.form.get("email") or "").strip() or None
        prefs.report_enabled = request.form.get("report_enabled") == "on"
        try:
            prefs.report_day = int(request.form.get("report_day", 0))
        except ValueError:
            prefs.report_day = 0
        try:
            prefs.report_hour = int(request.form.get("report_hour", 8))
        except ValueError:
            prefs.report_hour = 8

        included = request.form.getlist("report_include")
        prefs.report_include = json.dumps([s for s in included if s in SECTIONS])

        session.commit()
        prefs_data = {
            "email": prefs.email,
            "report_enabled": prefs.report_enabled,
            "report_day": prefs.report_day,
            "report_hour": prefs.report_hour,
        }
        included_list = json.loads(prefs.report_include or "[]")

    ctx = {
        "title": "Preferences",
        "prefs": prefs_data,
        "included": included_list,
        "day_names": DAY_NAMES,
        "sections": SECTIONS,
        "error": None,
        "message": "Preferences saved.",
    }

    if _is_htmx():
        resp = make_response(render_template("preferences.html", **ctx))
        resp.headers["HX-Trigger"] = json.dumps({"preferencesSaved": True})
        return resp
    return render_template("preferences.html", **ctx)


@bp.get("/report-preview")
def report_preview():
    """Render what the weekly email report would look like."""
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return "Database not configured.", 503

    with SessionLocal() as session:
        html = generate_weekly_report(session)
    return html
