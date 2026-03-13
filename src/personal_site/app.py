from __future__ import annotations

import os
import threading
import time
from collections import deque

from flask import Flask, jsonify, render_template, request

from . import ai_models  # noqa: F401
from . import caffeine_models  # noqa: F401
from . import sleep_models  # noqa: F401
from .ai import AiConfig, handle_ntfy_message
from .config import get_settings
from .db import Base, create_engine_and_sessionmaker, ensure_sqlite_workouts_schema
from .notify import NotificationError, NtfyConfig, listen_ntfy, send_ntfy
from .security import require_admin
from .caffeine import bp as caffeine_bp
from .sleep import bp as sleep_bp
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

    @app.get("/")
    def index():
        return render_template("index.html", title="Home")

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
            model=settings.openai_model,
            api_key=settings.openai_api_key,
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

    app.register_blueprint(workouts_bp)
    app.register_blueprint(sleep_bp)
    app.register_blueprint(caffeine_bp)

    return app
