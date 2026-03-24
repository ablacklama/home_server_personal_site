from __future__ import annotations

import datetime as dt

from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
)

from .stats_queries import nutrition_stats, sleep_stats, workout_stats

bp = Blueprint("stats", __name__, url_prefix="/stats")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _parse_period() -> tuple[dt.date, dt.date, str]:
    period = request.args.get("period", "week")
    date_raw = request.args.get("date", "")
    try:
        ref = dt.date.fromisoformat(date_raw) if date_raw else dt.date.today()
    except ValueError:
        ref = dt.date.today()

    if period == "month":
        start = ref.replace(day=1)
        # End of month
        if ref.month == 12:
            end = ref.replace(year=ref.year + 1, month=1, day=1) - dt.timedelta(days=1)
        else:
            end = ref.replace(month=ref.month + 1, day=1) - dt.timedelta(days=1)
    elif period == "year":
        start = ref.replace(month=1, day=1)
        end = ref.replace(month=12, day=31)
    else:
        # week
        period = "week"
        start = ref - dt.timedelta(days=ref.weekday())
        end = start + dt.timedelta(days=6)

    return start, end, period


@bp.get("/")
def index():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    start, end, period = _parse_period()

    if SessionLocal is None:
        return render_template(
            "stats.html",
            title="Stats",
            period=period,
            start=start.isoformat(),
            end=end.isoformat(),
            workouts={},
            sleep={},
            nutrition={},
        )

    with SessionLocal() as session:
        w = workout_stats(session, start, end)
        s = sleep_stats(session, start, end)
        n = nutrition_stats(session, start, end)

    return render_template(
        "stats.html",
        title="Stats",
        period=period,
        start=start.isoformat(),
        end=end.isoformat(),
        workouts=w,
        sleep=s,
        nutrition=n,
    )


@bp.get("/api/workouts")
def api_workouts():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return jsonify({}), 503
    start, end, period = _parse_period()
    with SessionLocal() as session:
        return jsonify(workout_stats(session, start, end))


@bp.get("/api/sleep")
def api_sleep():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return jsonify({}), 503
    start, end, period = _parse_period()
    with SessionLocal() as session:
        return jsonify(sleep_stats(session, start, end))


@bp.get("/api/nutrition")
def api_nutrition():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return jsonify({}), 503
    start, end, period = _parse_period()
    with SessionLocal() as session:
        return jsonify(nutrition_stats(session, start, end))
