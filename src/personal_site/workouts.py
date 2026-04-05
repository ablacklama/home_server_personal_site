from __future__ import annotations

import datetime as dt
import json
import re

from flask import (
    Blueprint,
    current_app,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, select

from .activity_log import log_activity
from .tz import now_pacific, today_pacific
from .workouts_models import WorkoutEntry, WorkoutType

bp = Blueprint("workouts", __name__, url_prefix="/workouts")


ALLOWED_METRIC_TYPES = {"string", "integer", "hours_minutes"}
ALLOWED_TIME_BUCKETS = {"morning", "afternoon", "night"}


def _boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "required"}
    return bool(value)


def _coerce_metric_value(expected_type: str, value: object) -> object:
    expected_type = (expected_type or "").strip().lower()
    if expected_type == "string":
        if value is None:
            raise ValueError("value is missing")
        return str(value)
    if expected_type == "integer":
        if isinstance(value, bool):
            raise ValueError("value must be an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip() != "":
            return int(value.strip())
        raise ValueError("value must be an integer")
    if expected_type == "hours_minutes":
        if not isinstance(value, dict):
            raise ValueError("value must be an object with hours/minutes")
        hours = int(value.get("hours") or 0)
        minutes = int(value.get("minutes") or 0)
        if hours < 0 or minutes < 0 or minutes > 59:
            raise ValueError("minutes must be 0-59 and hours >= 0")
        return {"hours": hours, "minutes": minutes}
    raise ValueError("unknown metric type")


def _parse_hours_minutes_text(raw: str) -> dict[str, int]:
    s = (raw or "").strip().lower()
    if not s:
        raise ValueError("value is missing")

    if ":" in s:
        parts = s.split(":", 1)
        if len(parts) != 2:
            raise ValueError("default must be H:MM")
        hours = int(parts[0].strip() or 0)
        minutes = int(parts[1].strip() or 0)
        return {"hours": hours, "minutes": minutes}

    m = re.match(
        r"^\s*(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?\s*$", s
    )
    if m and (m.group(1) or m.group(2)):
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        return {"hours": hours, "minutes": minutes}

    m = re.match(r"^\s*(\d+)\s*m\s*$", s)
    if m:
        total_minutes = int(m.group(1))
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return {"hours": hours, "minutes": minutes}

    raise ValueError("default must be H:MM, '2h 15m', or '150m'")


def _parse_default_value(metric_type: str, raw_default: object) -> object | None:
    if raw_default is None:
        return None

    if isinstance(raw_default, str):
        s = raw_default.strip()
        if s == "":
            return None
        raw_default = s

    metric_type = (metric_type or "").strip().lower()
    if metric_type == "string":
        return str(raw_default)
    if metric_type == "integer":
        return _coerce_metric_value("integer", raw_default)
    if metric_type == "hours_minutes":
        if isinstance(raw_default, dict):
            return _coerce_metric_value("hours_minutes", raw_default)
        if isinstance(raw_default, str):
            return _coerce_metric_value(
                "hours_minutes", _parse_hours_minutes_text(raw_default)
            )
        raise ValueError("default must be H:MM or an object with hours/minutes")
    raise ValueError("unknown metric type")


def _validate_and_apply_metrics(
    *,
    schema_items: list[dict],
    raw_metrics: dict[str, object],
) -> dict[str, object]:
    schema_map: dict[str, str] = {}
    required: set[str] = set()
    defaults: dict[str, object] = {}

    for item in schema_items or []:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        t = str(item.get("type") or "string").strip().lower()
        if t in {"text"}:
            t = "string"
        if t in {"number", "int"}:
            t = "integer"
        if t in {"hours", "duration", "time"}:
            t = "hours_minutes"
        if t not in ALLOWED_METRIC_TYPES:
            raise ValueError(f"Unknown metric type for '{key}'")

        schema_map[key] = t

        if _boolish(item.get("required")):
            required.add(key)

        if "default" in item:
            default_value = _parse_default_value(t, item.get("default"))
            if default_value is not None:
                defaults[key] = default_value

    validated: dict[str, object] = {}
    for key, expected_type in schema_map.items():
        raw_value = (raw_metrics or {}).get(key)
        if raw_value is not None and raw_value != "":
            try:
                validated[key] = _coerce_metric_value(expected_type, raw_value)
            except ValueError as exc:
                raise ValueError(f"'{key}' {exc}")
            continue

        if key in defaults:
            validated[key] = defaults[key]
            continue

        if key in required:
            raise ValueError(f"'{key}' is required")

    return validated


def _current_time_bucket(now: dt.datetime) -> str:
    hour = now.hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    return "night"


def _bucket_to_time(bucket: str) -> dt.time:
    # Representative times used to populate performed_at for ordering.
    if bucket == "morning":
        return dt.time(9, 0)
    if bucket == "afternoon":
        return dt.time(14, 0)
    return dt.time(20, 0)


def _parse_metric_schema(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    if not raw:
        return []

    if raw.startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("metric schema must be a JSON array")
        for item in parsed:
            if not isinstance(item, dict):
                raise ValueError("metric schema items must be objects")
            if "key" not in item:
                raise ValueError("metric schema items must include a 'key'")
            if "type" in item:
                t = str(item["type"]).strip().lower()
                if t in {"text"}:
                    t = "string"
                if t in {"number", "int"}:
                    t = "integer"
                if t in {"hours", "duration", "time"}:
                    t = "hours_minutes"
                if t not in ALLOWED_METRIC_TYPES:
                    raise ValueError(
                        "metric schema type must be one of: string, integer, hours_minutes"
                    )
                item["type"] = t
            else:
                item["type"] = "string"

            if "required" in item:
                item["required"] = _boolish(item.get("required"))

            if "default" in item:
                try:
                    default_value = _parse_default_value(
                        str(item.get("type") or "string"), item.get("default")
                    )
                except ValueError as exc:
                    raise ValueError(f"invalid default for '{item.get('key')}': {exc}")
                if default_value is None:
                    item.pop("default", None)
                else:
                    item["default"] = default_value
        return parsed

    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return [{"key": k, "type": "string"} for k in keys]


def _parse_schema_from_form() -> list[dict]:
    keys = [k.strip() for k in request.form.getlist("metric_key")]
    types = [t.strip().lower() for t in request.form.getlist("metric_type")]
    requireds = [r.strip() for r in request.form.getlist("metric_required")]
    defaults = request.form.getlist("metric_default")

    schema: list[dict] = []
    for idx, key in enumerate(keys):
        if not key:
            continue
        metric_type = types[idx] if idx < len(types) else "string"
        if metric_type not in ALLOWED_METRIC_TYPES:
            raise ValueError(
                "metric type must be one of: string, integer, hours_minutes"
            )

        required_flag = False
        if idx < len(requireds):
            required_flag = requireds[idx] in {"1", "true", "yes", "on"}

        default_raw = defaults[idx] if idx < len(defaults) else ""
        try:
            default_value = _parse_default_value(metric_type, default_raw)
        except ValueError as exc:
            raise ValueError(f"invalid default for '{key}': {exc}")

        item: dict[str, object] = {"key": key, "type": metric_type}
        if required_flag:
            item["required"] = True
        if default_value is not None:
            item["default"] = default_value
        schema.append(item)
    return schema


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
    types = session.scalars(select(WorkoutType).order_by(WorkoutType.name.asc())).all()
    raw_entries = session.execute(
        select(WorkoutEntry, WorkoutType)
        .join(WorkoutType, WorkoutEntry.workout_type_id == WorkoutType.id)
        .order_by(WorkoutEntry.performed_at.desc())
        .limit(25)
    ).all()

    recent: list[dict] = []
    for entry, wt in raw_entries:
        performed_on = entry.performed_on
        bucket = entry.time_bucket
        if performed_on is None:
            performed_on = entry.performed_at.date()
        if bucket is None:
            bucket = _current_time_bucket(entry.performed_at)

        metrics_display: list[tuple[str, str]] = []
        for key, value in (entry.metrics or {}).items():
            if isinstance(value, dict) and "hours" in value and "minutes" in value:
                h = int(value.get("hours") or 0)
                m = int(value.get("minutes") or 0)
                metrics_display.append((key, f"{h}h {m}m"))
            else:
                metrics_display.append((key, str(value)))

        recent.append(
            {
                "entry_id": entry.id,
                "type_name": wt.name,
                "performed_on": performed_on.isoformat(),
                "time_bucket": bucket,
                "metrics": metrics_display,
                "notes": entry.notes,
            }
        )

    now = now_pacific()
    return {
        "workout_types": types,
        "recent_workouts": recent,
        "default_performed_on": today_pacific().isoformat(),
        "default_time_bucket": _current_time_bucket(now),
        "error": error,
    }


def _render_index(message: str | None = None):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "workouts.html",
            title="Workouts",
            workout_types=[],
            recent_workouts=[],
            default_performed_on=today_pacific().isoformat(),
            default_time_bucket=_current_time_bucket(now_pacific()),
            error=message
            or current_app.config.get("DB_ERROR")
            or "DATABASE_URL is not set (configure SQLite to use the workout tracker)",
        )

    with SessionLocal() as session:
        ctx = _load_index_context(session, error=message)
    return render_template("workouts.html", title="Workouts", **ctx)


def _render_entry_response(
    session, message: str | None = None, status: int = 200, trigger: dict | None = None
):
    ctx = _load_index_context(session, error=message)
    ctx["oob"] = True
    return _make_htmx_response(
        "partials/workouts_entry_response.html", ctx, status=status, trigger=trigger
    )


def _load_type_context(session, error: str | None = None):
    types = session.scalars(select(WorkoutType).order_by(WorkoutType.name.asc())).all()
    return {"workout_types": types, "error": error}


def _render_type_new(message: str | None = None):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "workouts_type_new.html",
            title="Workout Types",
            workout_types=[],
            error=message
            or current_app.config.get("DB_ERROR")
            or "DATABASE_URL is not set (configure SQLite to use the workout tracker)",
        )

    with SessionLocal() as session:
        types = session.scalars(
            select(WorkoutType).order_by(WorkoutType.name.asc())
        ).all()
    return render_template(
        "workouts_type_new.html",
        title="Workout Types",
        workout_types=types,
        error=message,
    )


def _render_type_response(
    session, message: str | None = None, status: int = 200, trigger: dict | None = None
):
    ctx = _load_type_context(session, error=message)
    ctx["oob"] = True
    return _make_htmx_response(
        "partials/workouts_type_response.html", ctx, status=status, trigger=trigger
    )


@bp.get("/")
def index():
    return _render_index()


@bp.get("/types")
def types_index():
    return _render_type_new()


@bp.get("/types/new")
def type_new():
    return _render_type_new()


@bp.get("/types/<type_id>/edit")
def type_edit_form(type_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_type_new("Database is not configured"), 503

    with SessionLocal() as session:
        wt = session.get(WorkoutType, type_id)
        if wt is None:
            return _render_type_new("Workout type not found"), 404

        return render_template(
            "workouts_type_edit.html",
            title=f"Edit {wt.name}",
            wtype=wt,
            error=None,
        )


@bp.post("/types/<type_id>/edit")
def type_edit(type_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_type_new("Database is not configured"), 503

    with SessionLocal() as session:
        wt = session.get(WorkoutType, type_id)
        if wt is None:
            return _render_type_new("Workout type not found"), 404

        name = (request.form.get("name") or "").strip()
        if not name:
            return render_template(
                "workouts_type_edit.html",
                title=f"Edit {wt.name}",
                wtype=wt,
                error="Name is required",
            ), 400

        # Check for duplicate name (excluding self)
        existing = session.scalar(
            select(WorkoutType).where(
                WorkoutType.name == name, WorkoutType.id != type_id
            )
        )
        if existing is not None:
            return render_template(
                "workouts_type_edit.html",
                title=f"Edit {wt.name}",
                wtype=wt,
                error="A workout type with that name already exists",
            ), 400

        try:
            if request.form.getlist("metric_key"):
                schema = _parse_schema_from_form()
            else:
                schema = wt.metric_schema  # keep existing if nothing submitted
        except (ValueError, json.JSONDecodeError) as exc:
            return render_template(
                "workouts_type_edit.html",
                title=f"Edit {wt.name}",
                wtype=wt,
                error=f"Invalid metric schema: {exc}",
            ), 400

        cal_raw = (request.form.get("calories_per_hour") or "").strip()
        calories_per_hour = None
        if cal_raw:
            try:
                calories_per_hour = float(cal_raw)
            except ValueError:
                return render_template(
                    "workouts_type_edit.html",
                    title=f"Edit {wt.name}",
                    wtype=wt,
                    error="Calories/hour must be a number",
                ), 400

        wt.name = name
        wt.metric_schema = schema
        wt.calories_per_hour = calories_per_hour
        session.commit()

        log_activity("workout", "edit_type", f"id={type_id} name={name}")

    return redirect(url_for("workouts.types_index"))


@bp.post("/types")
def create_type():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured (set DATABASE_URL)"
        if _is_htmx():
            return _make_htmx_response(
                "partials/workouts_type_response.html",
                {"workout_types": [], "error": message, "oob": True},
                status=503,
            )
        return _render_type_new(message), 503
    with SessionLocal() as session:
        name = (request.form.get("name") or "").strip()
        schema_raw = request.form.get("metric_schema") or ""

        if not name:
            if _is_htmx():
                return _render_type_response(
                    session, "Workout type name is required", status=400
                )
            return _render_type_new("Workout type name is required"), 400

        try:
            if request.form.getlist("metric_key"):
                schema = _parse_schema_from_form()
            else:
                schema = _parse_metric_schema(schema_raw)
        except (ValueError, json.JSONDecodeError) as exc:
            message = f"Invalid metric schema: {exc}"
            if _is_htmx():
                return _render_type_response(session, message, status=400)
            return _render_type_new(message), 400

        existing = session.scalar(select(WorkoutType).where(WorkoutType.name == name))
        if existing is not None:
            if _is_htmx():
                return _render_type_response(
                    session, "A workout type with that name already exists", status=400
                )
            return _render_type_new("A workout type with that name already exists"), 400

        cal_raw = (request.form.get("calories_per_hour") or "").strip()
        calories_per_hour = None
        if cal_raw:
            try:
                calories_per_hour = float(cal_raw)
            except ValueError:
                message = "Calories/hour must be a number"
                if _is_htmx():
                    return _render_type_response(session, message, status=400)
                return _render_type_new(message), 400

        session.add(
            WorkoutType(
                name=name,
                metric_schema=schema,
                calories_per_hour=calories_per_hour,
            )
        )
        session.commit()

        log_activity("workout", "create_type", f"name={name}")

        if _is_htmx():
            return _render_type_response(session, trigger={"workoutTypeSaved": True})
        return redirect(url_for("workouts.index"))


@bp.post("/entries")
def create_entry():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured (set DATABASE_URL)"
        if _is_htmx():
            return _make_htmx_response(
                "partials/workouts_entry_response.html",
                {"recent_workouts": [], "error": message, "oob": True},
                status=503,
            )
        return _render_index(message), 503
    with SessionLocal() as session:
        type_id = (request.form.get("workout_type_id") or "").strip()
        performed_on_raw = (request.form.get("performed_on") or "").strip()
        time_bucket = (request.form.get("time_bucket") or "").strip().lower()
        notes = (request.form.get("notes") or "").strip() or None

        if not type_id:
            if _is_htmx():
                return _render_entry_response(
                    session, "Workout type is required", status=400
                )
            return _render_index("Workout type is required"), 400

        workout_type = session.get(WorkoutType, type_id)
        if workout_type is None:
            if _is_htmx():
                return _render_entry_response(
                    session, "Unknown workout type", status=400
                )
            return _render_index("Unknown workout type"), 400

        performed_on: dt.date
        if performed_on_raw:
            try:
                performed_on = dt.date.fromisoformat(performed_on_raw)
            except ValueError:
                if _is_htmx():
                    return _render_entry_response(
                        session, "performed_on must be a date", status=400
                    )
                return _render_index("performed_on must be a date"), 400
        else:
            performed_on = today_pacific()

        if not time_bucket:
            time_bucket = _current_time_bucket(now_pacific())
        if time_bucket not in ALLOWED_TIME_BUCKETS:
            message = "time bucket must be morning, afternoon, or night"
            if _is_htmx():
                return _render_entry_response(session, message, status=400)
            return _render_index(message), 400

        performed_at = dt.datetime.combine(
            performed_on,
            _bucket_to_time(time_bucket),
        ).replace(tzinfo=dt.timezone.utc)

        raw_metrics: dict[str, object] = {}
        for metric in workout_type.metric_schema:
            key = str(metric.get("key", "")).strip()
            if not key:
                continue

            metric_type = str(metric.get("type", "string")).strip().lower()
            if metric_type in {"text"}:
                metric_type = "string"
            if metric_type in {"number", "int"}:
                metric_type = "integer"
            if metric_type not in ALLOWED_METRIC_TYPES:
                message = f"Unknown metric type for '{key}'"
                if _is_htmx():
                    return _render_entry_response(session, message, status=400)
                return _render_index(message), 400

            if metric_type == "string":
                value = (request.form.get(f"metric__{key}") or "").strip()
                if value == "":
                    continue
                raw_metrics[key] = value
            elif metric_type == "integer":
                raw = (request.form.get(f"metric__{key}") or "").strip()
                if raw == "":
                    continue
                try:
                    raw_metrics[key] = int(raw)
                except ValueError:
                    message = f"'{key}' must be an integer"
                    if _is_htmx():
                        return _render_entry_response(session, message, status=400)
                    return _render_index(message), 400
            else:
                raw_h = (request.form.get(f"metric__{key}__hours") or "").strip()
                raw_m = (request.form.get(f"metric__{key}__minutes") or "").strip()
                if raw_h == "" and raw_m == "":
                    continue
                try:
                    hours = int(raw_h) if raw_h != "" else 0
                    minutes = int(raw_m) if raw_m != "" else 0
                except ValueError:
                    message = f"'{key}' hours/minutes must be integers"
                    if _is_htmx():
                        return _render_entry_response(session, message, status=400)
                    return _render_index(message), 400
                if hours < 0 or minutes < 0 or minutes > 59:
                    message = f"'{key}' minutes must be 0-59 and hours >= 0"
                    if _is_htmx():
                        return _render_entry_response(session, message, status=400)
                    return _render_index(message), 400
                raw_metrics[key] = {"hours": hours, "minutes": minutes}

        try:
            metrics = _validate_and_apply_metrics(
                schema_items=workout_type.metric_schema or [],
                raw_metrics=raw_metrics,
            )
        except ValueError as exc:
            if _is_htmx():
                return _render_entry_response(session, str(exc), status=400)
            return _render_index(str(exc)), 400

        session.add(
            WorkoutEntry(
                workout_type_id=workout_type.id,
                performed_at=performed_at,
                performed_on=performed_on,
                time_bucket=time_bucket,
                notes=notes,
                metrics=metrics,
            )
        )
        session.commit()

        log_activity("workout", "create", f"{workout_type.name} on {performed_on}")

        if _is_htmx():
            return _render_entry_response(session, trigger={"workoutEntrySaved": True})
        return redirect(url_for("workouts.index"))


@bp.get("/entries/<entry_id>/edit")
def entry_edit_form(entry_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_index("Database is not configured"), 503

    with SessionLocal() as session:
        row = session.execute(
            select(WorkoutEntry, WorkoutType)
            .join(WorkoutType, WorkoutEntry.workout_type_id == WorkoutType.id)
            .where(WorkoutEntry.id == entry_id)
            .limit(1)
        ).first()
        if row is None:
            return _render_index("Workout entry not found"), 404
        entry, wt = row
        performed_on = entry.performed_on or entry.performed_at.date()
        bucket = entry.time_bucket or _current_time_bucket(entry.performed_at)
        types = session.scalars(
            select(WorkoutType).order_by(WorkoutType.name.asc())
        ).all()

        return render_template(
            "workouts_entry_edit.html",
            title="Edit Workout",
            entry=entry,
            workout_type=wt,
            workout_types=types,
            performed_on=performed_on.isoformat(),
            time_bucket=bucket,
        )


@bp.post("/entries/<entry_id>/edit")
def entry_edit(entry_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_index("Database is not configured"), 503

    with SessionLocal() as session:
        entry = session.get(WorkoutEntry, entry_id)
        if entry is None:
            if _is_htmx():
                return _render_entry_response(
                    session, "Workout entry not found", status=404
                )
            return _render_index("Workout entry not found"), 404

        type_id = (request.form.get("workout_type_id") or "").strip()
        performed_on_raw = (request.form.get("performed_on") or "").strip()
        time_bucket = (request.form.get("time_bucket") or "").strip().lower()
        notes = (request.form.get("notes") or "").strip() or None

        if not type_id:
            return _render_index("Workout type is required"), 400

        workout_type = session.get(WorkoutType, type_id)
        if workout_type is None:
            return _render_index("Unknown workout type"), 400

        if performed_on_raw:
            try:
                performed_on = dt.date.fromisoformat(performed_on_raw)
            except ValueError:
                return _render_index("performed_on must be a date"), 400
        else:
            performed_on = entry.performed_on or entry.performed_at.date()

        if not time_bucket:
            time_bucket = entry.time_bucket or _current_time_bucket(now_pacific())
        if time_bucket not in ALLOWED_TIME_BUCKETS:
            return (
                _render_index("time bucket must be morning, afternoon, or night"),
                400,
            )

        performed_at = dt.datetime.combine(
            performed_on, _bucket_to_time(time_bucket)
        ).replace(tzinfo=dt.timezone.utc)

        raw_metrics: dict[str, object] = {}
        for metric in workout_type.metric_schema:
            key = str(metric.get("key", "")).strip()
            if not key:
                continue
            metric_type = str(metric.get("type", "string")).strip().lower()
            if metric_type in {"text"}:
                metric_type = "string"
            if metric_type in {"number", "int"}:
                metric_type = "integer"

            if metric_type == "hours_minutes":
                raw_h = (request.form.get(f"metric__{key}__hours") or "").strip()
                raw_m = (request.form.get(f"metric__{key}__minutes") or "").strip()
                if raw_h == "" and raw_m == "":
                    continue
                try:
                    hours = int(raw_h) if raw_h != "" else 0
                    minutes = int(raw_m) if raw_m != "" else 0
                except ValueError:
                    return _render_index(f"'{key}' hours/minutes must be integers"), 400
                if hours < 0 or minutes < 0 or minutes > 59:
                    return (
                        _render_index(f"'{key}' minutes must be 0-59 and hours >= 0"),
                        400,
                    )
                raw_metrics[key] = {"hours": hours, "minutes": minutes}
            elif metric_type == "integer":
                raw = (request.form.get(f"metric__{key}") or "").strip()
                if raw == "":
                    continue
                try:
                    raw_metrics[key] = int(raw)
                except ValueError:
                    return _render_index(f"'{key}' must be an integer"), 400
            else:
                value = (request.form.get(f"metric__{key}") or "").strip()
                if value:
                    raw_metrics[key] = value

        try:
            metrics = _validate_and_apply_metrics(
                schema_items=workout_type.metric_schema or [],
                raw_metrics=raw_metrics,
            )
        except ValueError as exc:
            return _render_index(str(exc)), 400

        entry.workout_type_id = workout_type.id
        entry.performed_at = performed_at
        entry.performed_on = performed_on
        entry.time_bucket = time_bucket
        entry.notes = notes
        entry.metrics = metrics
        session.commit()

    log_activity("workout", "edit", f"id={entry_id} type={workout_type.name}")
    return redirect(url_for("workouts.index"))


@bp.get("/entries/<entry_id>/delete")
def entry_delete_confirm(entry_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_index("Database is not configured (set DATABASE_URL)"), 503

    with SessionLocal() as session:
        row = session.execute(
            select(WorkoutEntry, WorkoutType)
            .join(WorkoutType, WorkoutEntry.workout_type_id == WorkoutType.id)
            .where(WorkoutEntry.id == entry_id)
            .limit(1)
        ).first()
        if row is None:
            return _render_index("Workout entry not found"), 404
        entry, wt = row
        performed_on = entry.performed_on or entry.performed_at.date()
        bucket = entry.time_bucket or _current_time_bucket(entry.performed_at)

        return render_template(
            "workouts_entry_delete.html",
            title="Delete workout",
            entry_id=entry.id,
            type_name=wt.name,
            performed_on=performed_on.isoformat(),
            time_bucket=bucket,
        )


@bp.post("/entries/<entry_id>/delete")
def entry_delete(entry_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured (set DATABASE_URL)"
        if _is_htmx():
            return _make_htmx_response(
                "partials/workouts_entry_response.html",
                {"recent_workouts": [], "error": message, "oob": True},
                status=503,
            )
        return _render_index(message), 503

    with SessionLocal() as session:
        entry = session.get(WorkoutEntry, entry_id)
        if entry is None:
            if _is_htmx():
                return _render_entry_response(
                    session, "Workout entry not found", status=404
                )
            return _render_index("Workout entry not found"), 404
        session.delete(entry)
        session.commit()

        log_activity("workout", "delete", f"id={entry_id}")

        if _is_htmx():
            return _render_entry_response(session)
    return redirect(url_for("workouts.index"))


@bp.get("/types/<type_id>/delete")
def type_delete_confirm(type_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_type_new("Database is not configured (set DATABASE_URL)"), 503

    with SessionLocal() as session:
        wt = session.get(WorkoutType, type_id)
        if wt is None:
            return _render_type_new("Workout type not found"), 404

        entry_count = session.scalar(
            select(func.count())
            .select_from(WorkoutEntry)
            .where(WorkoutEntry.workout_type_id == wt.id)
        )

        return render_template(
            "workouts_type_delete.html",
            title="Delete workout type",
            type_id=wt.id,
            type_name=wt.name,
            entry_count=int(entry_count or 0),
        )


@bp.post("/types/<type_id>/delete")
def type_delete(type_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured (set DATABASE_URL)"
        if _is_htmx():
            return _make_htmx_response(
                "partials/workouts_type_response.html",
                {"workout_types": [], "error": message, "oob": True},
                status=503,
            )
        return _render_type_new(message), 503

    with SessionLocal() as session:
        wt = session.get(WorkoutType, type_id)
        if wt is None:
            if _is_htmx():
                return _render_type_response(
                    session, "Workout type not found", status=404
                )
            return _render_type_new("Workout type not found"), 404
        name = wt.name
        session.delete(wt)
        session.commit()

        log_activity("workout", "delete_type", f"id={type_id} name={name}")

        if _is_htmx():
            return _render_type_response(session)
    return redirect(url_for("workouts.types_index"))
