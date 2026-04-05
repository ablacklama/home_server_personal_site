from __future__ import annotations

import datetime as dt

from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
)

from .stats_queries import (
    nutrition_stats,
    pickleball_insights,
    sleep_stats,
    workout_stats,
)
from .tz import today_pacific

bp = Blueprint("stats", __name__, url_prefix="/stats")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _client_today() -> dt.date:
    """Always use Pacific time for the current date."""
    return today_pacific()


def _parse_period() -> tuple[dt.date, dt.date, str]:
    period = request.args.get("period", "week")
    date_raw = request.args.get("date", "")
    today = _client_today()
    try:
        ref = dt.date.fromisoformat(date_raw) if date_raw else today
    except ValueError:
        ref = today

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

    today = _client_today()
    with SessionLocal() as session:
        w = workout_stats(session, start, end, today=today)
        s = sleep_stats(session, start, end, today=today)
        n = nutrition_stats(session, start, end, today=today)

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


@bp.get("/sleep")
def sleep_detail():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    start, end, period = _parse_period()
    today = _client_today()

    if SessionLocal is None:
        return render_template(
            "stats_sleep.html",
            title="Sleep Stats",
            period=period,
            start=start.isoformat(),
            end=end.isoformat(),
            sleep={},
        )

    with SessionLocal() as session:
        s = sleep_stats(session, start, end, today=today)

    return render_template(
        "stats_sleep.html",
        title="Sleep Stats",
        period=period,
        start=start.isoformat(),
        end=end.isoformat(),
        sleep=s,
    )


@bp.get("/nutrition")
def nutrition_detail():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    start, end, period = _parse_period()
    today = _client_today()

    if SessionLocal is None:
        return render_template(
            "stats_nutrition.html",
            title="Nutrition Stats",
            period=period,
            start=start.isoformat(),
            end=end.isoformat(),
            nutrition={},
        )

    with SessionLocal() as session:
        n = nutrition_stats(session, start, end, today=today)

    return render_template(
        "stats_nutrition.html",
        title="Nutrition Stats",
        period=period,
        start=start.isoformat(),
        end=end.isoformat(),
        nutrition=n,
    )


@bp.get("/workouts")
def workouts_detail():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    start, end, period = _parse_period()
    today = _client_today()

    if SessionLocal is None:
        return render_template(
            "stats_workouts.html",
            title="Workout Stats",
            period=period,
            start=start.isoformat(),
            end=end.isoformat(),
            workouts={},
        )

    with SessionLocal() as session:
        w = workout_stats(session, start, end, today=today)

    return render_template(
        "stats_workouts.html",
        title="Workout Stats",
        period=period,
        start=start.isoformat(),
        end=end.isoformat(),
        workouts=w,
    )


@bp.get("/api/workouts")
def api_workouts():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return jsonify({}), 503
    start, end, period = _parse_period()
    today = _client_today()
    with SessionLocal() as session:
        return jsonify(workout_stats(session, start, end, today=today))


@bp.get("/api/sleep")
def api_sleep():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return jsonify({}), 503
    start, end, period = _parse_period()
    today = _client_today()
    with SessionLocal() as session:
        return jsonify(sleep_stats(session, start, end, today=today))


@bp.get("/api/nutrition")
def api_nutrition():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return jsonify({}), 503
    start, end, period = _parse_period()
    today = _client_today()
    with SessionLocal() as session:
        return jsonify(nutrition_stats(session, start, end, today=today))


@bp.get("/pickleball")
def pickleball_detail():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "stats_pickleball.html",
            title="Pickleball Insights",
            insights={"has_data": False},
        )

    with SessionLocal() as session:
        insights = pickleball_insights(session)

    return render_template(
        "stats_pickleball.html",
        title="Pickleball Insights",
        insights=insights,
    )
