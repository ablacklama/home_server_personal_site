from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import traceback

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

from .ai import AiConfig, handle_chat_message
from .ai_models import AiMessage, ChatConversation

logger = logging.getLogger(__name__)

bp = Blueprint("chat", __name__, url_prefix="/chat")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _make_htmx_response(
    template_name: str, context: dict, status: int = 200, trigger: dict | None = None
):
    response = make_response(render_template(template_name, **context), status)
    if trigger:
        response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


def _get_conversations(session) -> list[ChatConversation]:
    return list(
        session.scalars(
            select(ChatConversation).order_by(ChatConversation.updated_at.desc())
        ).all()
    )


def _get_messages(session, conversation_id: str) -> list[AiMessage]:
    return list(
        session.scalars(
            select(AiMessage)
            .where(AiMessage.conversation_id == conversation_id)
            .order_by(AiMessage.created_at.asc())
        ).all()
    )


@bp.get("/")
def index():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "chat.html",
            title="Chat",
            conversations=[],
            active_conversation=None,
            messages=[],
            error="Database not configured",
        )

    with SessionLocal() as session:
        conversations = _get_conversations(session)
        active = conversations[0] if conversations else None
        messages = _get_messages(session, active.id) if active else []

    return render_template(
        "chat.html",
        title="Chat",
        conversations=conversations,
        active_conversation=active,
        messages=messages,
    )


@bp.post("/conversations")
def create_conversation():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return redirect(url_for("chat.index"))

    with SessionLocal() as session:
        today = dt.date.today()
        date_label = today.strftime("%b %-d")
        # Count how many conversations were created today
        start_of_day = dt.datetime.combine(today, dt.time.min, tzinfo=dt.timezone.utc)
        count_today = (
            session.scalar(
                select(func.count(ChatConversation.id)).where(
                    ChatConversation.created_at >= start_of_day
                )
            )
            or 0
        )
        title = f"{date_label} #{count_today + 1}"

        conv = ChatConversation(title=title)
        session.add(conv)
        session.commit()
        conv_id = conv.id

    return redirect(url_for("chat.conversation", conv_id=conv_id))


@bp.get("/conversations/<conv_id>")
def conversation(conv_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return redirect(url_for("chat.index"))

    with SessionLocal() as session:
        conv = session.get(ChatConversation, conv_id)
        if conv is None:
            return redirect(url_for("chat.index"))
        conversations = _get_conversations(session)
        messages = _get_messages(session, conv.id)

    if _is_htmx():
        return _make_htmx_response(
            "partials/chat_messages.html",
            {
                "messages": messages,
                "active_conversation": conv,
            },
        )

    return render_template(
        "chat.html",
        title="Chat",
        conversations=conversations,
        active_conversation=conv,
        messages=messages,
    )


@bp.post("/conversations/<conv_id>/messages")
def send_message(conv_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _make_htmx_response(
            "partials/chat_message.html",
            {"msg": None, "error": "Database not configured"},
            status=503,
        )

    text = (request.form.get("message") or "").strip()
    if not text:
        return _make_htmx_response(
            "partials/chat_message.html",
            {"msg": None, "error": "Message cannot be empty"},
            status=400,
        )

    settings = current_app.config.get("_settings")
    ai_cfg = AiConfig(
        enabled=settings.ai_enabled if settings else False,
        model=settings.anthropic_model if settings else "claude-sonnet-4-6",
        api_key=settings.anthropic_api_key if settings else None,
        debug_log=settings.ai_debug_log if settings else False,
    )

    with SessionLocal() as session:
        conv = session.get(ChatConversation, conv_id)
        if conv is None:
            return _make_htmx_response(
                "partials/chat_message.html",
                {"msg": None, "error": "Conversation not found"},
                status=404,
            )

        # Store user message
        user_msg = AiMessage(
            conversation_id=conv_id,
            role="user",
            content=text,
        )
        session.add(user_msg)
        session.commit()

        user_msg_data = {
            "id": user_msg.id,
            "role": "user",
            "content": text,
        }

    # Kick off AI response in background
    def _process():
        try:
            with SessionLocal() as session:
                handle_chat_message(
                    session=session,
                    ai_cfg=ai_cfg,
                    conversation_id=conv_id,
                )
        except Exception:
            tb = traceback.format_exc()
            logger.exception("Chat AI processing failed for %s", conv_id)
            try:
                with SessionLocal() as session:
                    # Clear any leftover status messages
                    stale = session.scalars(
                        select(AiMessage)
                        .where(AiMessage.conversation_id == conv_id)
                        .where(AiMessage.meta["kind"].as_string() == "status")
                    ).all()
                    for s in stale:
                        session.delete(s)
                    # Save error as visible message
                    session.add(
                        AiMessage(
                            conversation_id=conv_id,
                            role="assistant",
                            content=f"Error processing your message:\n\n{tb}",
                            meta={"kind": "error"},
                        )
                    )
                    session.commit()
            except Exception:
                logger.exception("Failed to save error message for %s", conv_id)

    threading.Thread(target=_process, name=f"chat-{conv_id}", daemon=True).start()

    # Return the user message immediately
    response = make_response(
        render_template(
            "partials/chat_message.html",
            msg=user_msg_data,
        )
    )
    response.headers["HX-Trigger"] = json.dumps({"chatMessageSent": True})
    return response


@bp.get("/conversations/<conv_id>/poll")
def poll_messages(conv_id: str):
    """Polling endpoint: returns messages after a given ID."""
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return ""

    after = request.args.get("after", "")

    with SessionLocal() as session:
        query = (
            select(AiMessage)
            .where(AiMessage.conversation_id == conv_id)
            .order_by(AiMessage.created_at.asc())
        )

        if after:
            # Get the timestamp of the "after" message
            after_msg = session.get(AiMessage, after)
            if after_msg:
                query = query.where(AiMessage.created_at > after_msg.created_at)

        messages = list(session.scalars(query).all())

    if not messages:
        return ""

    return render_template("partials/chat_new_messages.html", messages=messages)
