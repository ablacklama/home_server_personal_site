from __future__ import annotations

import os
import threading
import time
from collections import deque

import datetime as dt

from flask import Flask, jsonify, render_template, request
from sqlalchemy import select

from . import ai_models  # noqa: F401
from . import caffeine_models  # noqa: F401
from . import nutrition_models  # noqa: F401
from . import preferences_models  # noqa: F401
from . import sleep_models  # noqa: F401
from .ai import AiConfig, handle_ntfy_message
from .config import get_settings
from .db import (
    Base,
    create_engine_and_sessionmaker,
    ensure_sqlite_chat_schema,
    ensure_sqlite_goals_schema,
    ensure_sqlite_workouts_schema,
)
from .notify import NotificationError, NtfyConfig, listen_ntfy, send_ntfy
from .security import require_admin
from . import activity_log as _activity_log_models  # noqa: F401
from .caffeine import bp as caffeine_bp
from .chat import bp as chat_bp
from .logs import bp as logs_bp
from .nutrition import bp as nutrition_bp
from .preferences import bp as preferences_bp
from .sleep import bp as sleep_bp
from .stats import bp as stats_bp
from .workouts import bp as workouts_bp


def _should_start_background_threads(debug: bool) -> bool:
    if not debug:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def create_app() -> Flask:
    settings = get_settings()
    app = Flask(__name__)
    app.config["ADMIN_TOKEN"] = settings.admin_token

    app.session = None  # type: ignore[attr-defined]
    app.config["DB_ERROR"] = None
    if settings.database_url:
        try:
            engine, SessionLocal = create_engine_and_sessionmaker(settings.database_url)
            Base.metadata.create_all(bind=engine)
            ensure_sqlite_workouts_schema(engine)
            ensure_sqlite_chat_schema(engine)
            ensure_sqlite_goals_schema(engine)
            app.session = SessionLocal  # type: ignore[attr-defined]
        except Exception:
            app.logger.exception("Database initialization failed")
            app.config["DB_ERROR"] = "Database connection failed. Check DATABASE_URL."

    last_activity_lock = threading.Lock()
    last_activity_monotonic = time.monotonic()
    last_idle_notification_monotonic = 0.0

    sent_ntfy_ids_lock = threading.Lock()
    sent_ntfy_ids: deque[str] = deque(maxlen=200)

    processed_ntfy_ids_lock = threading.Lock()
    processed_ntfy_ids: deque[str] = deque(maxlen=500)

    def _remember_sent_ntfy_id(msg_id: str | None) -> None:
        if not msg_id:
            return
        with sent_ntfy_ids_lock:
            sent_ntfy_ids.append(msg_id)

    def _was_sent_by_us(msg_id: str | None) -> bool:
        if not msg_id:
            return False
        with sent_ntfy_ids_lock:
            return msg_id in sent_ntfy_ids

    def _remember_processed_ntfy_id(msg_id: str | None) -> None:
        if not msg_id:
            return
        with processed_ntfy_ids_lock:
            processed_ntfy_ids.append(msg_id)

    def _was_already_processed(msg_id: str | None) -> bool:
        if not msg_id:
            return False
        with processed_ntfy_ids_lock:
            return msg_id in processed_ntfy_ids

    def _is_self_generated_event(event: dict) -> bool:
        tags = event.get("tags")
        if isinstance(tags, list) and "ai" in tags:
            return True
        return False

    @app.before_request
    def _track_activity():
        nonlocal last_activity_monotonic
        if request.path.startswith("/static/"):
            return
        if request.path in {"/healthz"}:
            return
        with last_activity_lock:
            last_activity_monotonic = time.monotonic()

    def _client_today() -> dt.date:
        """Derive the client's local date from tz_offset query param."""
        raw = request.args.get("tz_offset", "")
        if raw:
            try:
                offset_min = int(raw)
                client_tz = dt.timezone(dt.timedelta(minutes=-offset_min))
                return dt.datetime.now(client_tz).date()
            except (ValueError, OverflowError):
                pass
        return dt.date.today()

    @app.get("/")
    def index():
        from .caffeine_models import CaffeineEntry
        from .nutrition_models import NutritionLog
        from .preferences_models import UserPreferences
        from .sleep_models import SleepEntry
        from .workouts_models import WorkoutEntry, WorkoutType

        # Support explicit date param for day navigation, else use client timezone
        date_raw = (request.args.get("date") or "").strip()
        if date_raw:
            try:
                day = dt.date.fromisoformat(date_raw)
            except ValueError:
                day = _client_today()
        else:
            day = _client_today()

        client_today = _client_today()
        is_today = day == client_today
        prev_day = (day - dt.timedelta(days=1)).isoformat()
        next_day = (day + dt.timedelta(days=1)).isoformat() if day < client_today else None
        today_label = day.strftime("%A, %B %-d")

        default_goals = {
            "calories": None,
            "protein_g": None,
            "carbs_g": None,
            "fat_g": None,
            "sleep_hours": None,
            "workouts_per_week": None,
            "caffeine_mg": None,
        }

        empty = {
            "title": "Home",
            "today_label": today_label,
            "sleep": None,
            "nutrition": {
                "calories": 0,
                "protein_g": 0,
                "carbs_g": 0,
                "fat_g": 0,
                "sugar_g": 0,
                "meals": [],
            },
            "workouts": [],
            "caffeine_mg": 0,
            "goals": default_goals,
        }

        SessionLocal = app.session  # type: ignore[attr-defined]
        if SessionLocal is None:
            return render_template(
                "home.html",
                **empty,
                prev_day=prev_day,
                next_day=next_day,
                is_today=is_today,
            )

        with SessionLocal() as session:
            # Sleep
            sleep_entry = session.scalars(
                select(SleepEntry)
                .where(SleepEntry.slept_on == day)
                .order_by(SleepEntry.created_at.desc())
                .limit(1)
            ).first()
            sleep_data = None
            if sleep_entry:
                sleep_data = {
                    "hours": sleep_entry.duration_minutes // 60,
                    "minutes": sleep_entry.duration_minutes % 60,
                    "quality": sleep_entry.quality,
                }

            # Nutrition
            logs = list(
                session.scalars(
                    select(NutritionLog).where(NutritionLog.logged_on == today)
                ).all()
            )
            total_cal = 0.0
            total_p = 0.0
            total_c = 0.0
            total_f = 0.0
            total_s = 0.0
            meal_summaries: list[dict] = []
            for log in logs:
                cal = log.total_calories
                total_cal += cal
                total_p += log.total_protein_g
                total_c += log.total_carbs_g
                total_f += log.total_fat_g
                total_s += log.total_sugar_g
                name = log.meal.name if log.meal else "Ad-hoc"
                if not log.meal and log.items:
                    names = [it.ingredient.name for it in log.items[:3]]
                    name = ", ".join(names)
                    if len(log.items) > 3:
                        name += f" +{len(log.items) - 3}"
                meal_summaries.append(
                    {
                        "name": name,
                        "time_bucket": log.time_bucket,
                        "calories": cal,
                    }
                )
            nutrition_data = {
                "calories": total_cal,
                "protein_g": total_p,
                "carbs_g": total_c,
                "fat_g": total_f,
                "sugar_g": total_s,
                "meals": meal_summaries,
            }

            # Workouts
            workout_entries = list(
                session.scalars(
                    select(WorkoutEntry).where(WorkoutEntry.performed_on == today)
                ).all()
            )
            type_ids = {e.workout_type_id for e in workout_entries}
            type_names = {}
            if type_ids:
                for wt in session.scalars(
                    select(WorkoutType).where(WorkoutType.id.in_(type_ids))
                ).all():
                    type_names[wt.id] = wt.name
            workout_data = [
                {
                    "name": type_names.get(e.workout_type_id, "Workout"),
                    "time_bucket": e.time_bucket or "",
                }
                for e in workout_entries
            ]

            # Caffeine
            caffeine_total = sum(
                e.amount_mg
                for e in session.scalars(
                    select(CaffeineEntry).where(CaffeineEntry.consumed_on == today)
                ).all()
            )

            # Goals
            prefs = session.scalars(select(UserPreferences).limit(1)).first()
            goals = dict(default_goals)
            if prefs:
                goals["calories"] = prefs.goal_calories
                goals["protein_g"] = prefs.goal_protein_g
                goals["carbs_g"] = prefs.goal_carbs_g
                goals["fat_g"] = prefs.goal_fat_g
                goals["sleep_hours"] = prefs.goal_sleep_hours
                goals["workouts_per_week"] = prefs.goal_workouts_per_week
                goals["caffeine_mg"] = prefs.goal_caffeine_mg

            # Weekly workout count (for goal progress)
            week_start = today - dt.timedelta(days=today.weekday())
            workouts_this_week = len(
                list(
                    session.scalars(
                        select(WorkoutEntry).where(
                            WorkoutEntry.performed_on >= week_start,
                            WorkoutEntry.performed_on <= today,
                        )
                    ).all()
                )
            )

        return render_template(
            "home.html",
            title="Home",
            today_label=today_label,
            sleep=sleep_data,
            nutrition=nutrition_data,
            workouts=workout_data,
            caffeine_mg=caffeine_total,
            goals=goals,
            workouts_this_week=workouts_this_week,
        )

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.post("/admin/notify-test")
    @require_admin
    def notify_test():
        if not settings.notify_enabled:
            return jsonify(error="NOTIFY_ENABLED is false"), 503

        cfg = NtfyConfig(
            base_url=settings.ntfy_base_url,
            topic=settings.ntfy_topic,
            token=settings.ntfy_token,
            user=settings.ntfy_user,
            password=settings.ntfy_password,
        )

        message = request.json.get("message") if request.is_json else None
        if not message:
            message = "Test notification from personal_site"

        try:
            msg_id = send_ntfy(
                config=cfg,
                title="personal_site",
                message=message,
                tags=["test"],
                priority=3,
            )
            _remember_sent_ntfy_id(msg_id)
        except NotificationError as exc:
            return jsonify(error=str(exc)), 502

        return jsonify(ok=True)

    def _inactivity_watcher():
        nonlocal last_idle_notification_monotonic
        cfg = NtfyConfig(
            base_url=settings.ntfy_base_url,
            topic=settings.ntfy_topic,
            token=settings.ntfy_token,
            user=settings.ntfy_user,
            password=settings.ntfy_password,
        )

        while True:
            time.sleep(30)

            with last_activity_lock:
                idle_for = time.monotonic() - last_activity_monotonic
                since_last_sent = time.monotonic() - last_idle_notification_monotonic

            if idle_for < settings.inactivity_seconds:
                continue
            if since_last_sent < settings.inactivity_cooldown_seconds:
                continue

            try:
                minutes = int(idle_for // 60)
                msg_id = send_ntfy(
                    config=cfg,
                    title="personal_site idle",
                    message=f"No site activity for ~{minutes} minutes.",
                    tags=["idle"],
                    priority=2,
                )
                _remember_sent_ntfy_id(msg_id)
            except NotificationError:
                continue

            with last_activity_lock:
                last_idle_notification_monotonic = time.monotonic()

    if (
        settings.notify_enabled
        and settings.inactivity_notify_enabled
        and settings.ntfy_topic
        and _should_start_background_threads(settings.debug)
    ):
        threading.Thread(
            target=_inactivity_watcher, name="inactivity-watcher", daemon=True
        ).start()

    def _ntfy_listener():
        if not settings.ntfy_topic:
            return

        cfg = NtfyConfig(
            base_url=settings.ntfy_base_url,
            topic=settings.ntfy_topic,
            token=settings.ntfy_token,
            user=settings.ntfy_user,
            password=settings.ntfy_password,
        )

        ai_cfg = AiConfig(
            enabled=settings.ai_enabled,
            model=settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            debug_log=settings.ai_debug_log,
        )

        def _process_event(event: dict) -> None:
            SessionLocal = app.session  # type: ignore[attr-defined]
            if SessionLocal is None:
                return
            message = str(event.get("message") or "").strip()
            if not message:
                return
            try:
                with SessionLocal() as session:
                    result = handle_ntfy_message(
                        session=session,
                        ntfy_cfg=cfg,
                        ai_cfg=ai_cfg,
                        topic=cfg.topic,
                        text=message,
                        received_event=event,
                        remember_sent_ntfy_id=_remember_sent_ntfy_id,
                    )
                app.logger.info("ai ntfy handled: %s", result)
            except Exception:
                app.logger.exception("ai ntfy processing failed")

        backoff_s = 2
        while True:
            try:
                for event in listen_ntfy(config=cfg):
                    msg_id = str(event.get("id") or "")
                    if _was_sent_by_us(msg_id):
                        continue

                    if _is_self_generated_event(event):
                        # Secondary guard: if we ever miss remembering the sent id,
                        # don't let the AI respond to its own messages.
                        continue

                    if _was_already_processed(msg_id):
                        continue
                    _remember_processed_ntfy_id(msg_id)

                    title = event.get("title")
                    message = event.get("message")
                    tags = event.get("tags")
                    app.logger.info(
                        "ntfy incoming (external): id=%s title=%s tags=%s message=%s",
                        msg_id,
                        title,
                        tags,
                        message,
                    )

                    if settings.ai_enabled:
                        threading.Thread(
                            target=_process_event,
                            args=(event,),
                            name=f"ai-ntfy-{msg_id or 'msg'}",
                            daemon=True,
                        ).start()

                backoff_s = 2
            except NotificationError:
                app.logger.exception("ntfy listener error")
                time.sleep(backoff_s)
                backoff_s = min(60, backoff_s * 2)

    if settings.ntfy_topic and _should_start_background_threads(settings.debug):
        threading.Thread(
            target=_ntfy_listener, name="ntfy-listener", daemon=True
        ).start()

    def _report_scheduler():
        import datetime as _dt

        from sqlalchemy import select as _select

        from .preferences_models import UserPreferences
        from .reports import generate_weekly_report, send_report

        last_sent_date: _dt.date | None = None

        while True:
            time.sleep(300)  # check every 5 minutes

            SessionLocal = app.session  # type: ignore[attr-defined]
            if SessionLocal is None:
                continue

            try:
                now = _dt.datetime.now()
                today = now.date()

                if last_sent_date == today:
                    continue

                with SessionLocal() as session:
                    prefs = session.scalars(_select(UserPreferences).limit(1)).first()
                    if not prefs or not prefs.report_enabled:
                        continue
                    if not prefs.email:
                        continue

                    if now.weekday() != prefs.report_day:
                        continue
                    if now.hour < prefs.report_hour:
                        continue

                    html = generate_weekly_report(session)

                send_report(html, prefs.email)
                last_sent_date = today
                app.logger.info("Weekly report sent to %s", prefs.email)
            except Exception:
                app.logger.exception("Report scheduler error")

    if _should_start_background_threads(settings.debug):
        threading.Thread(
            target=_report_scheduler, name="report-scheduler", daemon=True
        ).start()

    app.config["_settings"] = settings

    # Markdown filter for chat messages
    import markdown as _md

    def _md_filter(text: str) -> str:
        return _md.markdown(text, extensions=["tables", "fenced_code", "nl2br"])

    app.jinja_env.filters["markdown"] = _md_filter

    app.register_blueprint(workouts_bp)
    app.register_blueprint(sleep_bp)
    app.register_blueprint(caffeine_bp)
    app.register_blueprint(nutrition_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(preferences_bp)
    app.register_blueprint(logs_bp)

    return app
