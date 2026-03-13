from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from openai import OpenAI
from sqlalchemy import func, select

from .ai_models import AiMessage, AiPending
from .notify import NotificationError, NtfyConfig, send_ntfy
from .workouts import ALLOWED_TIME_BUCKETS, _bucket_to_time, _current_time_bucket
from .caffeine_models import CaffeineEntry
from .sleep_models import SleepEntry
from .workouts_models import WorkoutEntry, WorkoutType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiConfig:
    enabled: bool
    model: str
    api_key: str | None
    debug_log: bool = False


def _tool_schema_metric_value() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "description": "Metric key name."},
            "value_string": {"type": ["string", "null"]},
            "value_integer": {"type": ["integer", "null"]},
            "value_hours_minutes": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "hours": {"type": "integer"},
                    "minutes": {"type": "integer"},
                },
                "required": ["hours", "minutes"],
            },
        },
        "required": ["key", "value_string", "value_integer", "value_hours_minutes"],
    }


def _tool_schema_partial_workout() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workout_type_name": {"type": ["string", "null"]},
            "performed_on": {
                "type": ["string", "null"],
                "description": "ISO date YYYY-MM-DD, or null to mean today.",
            },
            "time_bucket": {
                "type": ["string", "null"],
                "enum": [None, "morning", "afternoon", "night"],
            },
            "notes": {"type": ["string", "null"]},
            "metrics": {
                "type": "array",
                "items": _tool_schema_metric_value(),
            },
        },
        "required": [
            "workout_type_name",
            "performed_on",
            "time_bucket",
            "notes",
            "metrics",
        ],
    }


def _tool_schema_partial_sleep() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "slept_on": {
                "type": ["string", "null"],
                "description": "ISO date YYYY-MM-DD, or null to mean today.",
            },
            "duration_hours": {"type": ["integer", "null"]},
            "duration_minutes": {"type": ["integer", "null"]},
            "quality": {"type": ["integer", "null"], "description": "1-5 scale."},
            "notes": {"type": ["string", "null"]},
        },
        "required": [
            "slept_on",
            "duration_hours",
            "duration_minutes",
            "quality",
            "notes",
        ],
    }


def _tool_schema_partial_caffeine() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "consumed_on": {
                "type": ["string", "null"],
                "description": "ISO date YYYY-MM-DD, or null to mean today.",
            },
            "time_bucket": {
                "type": ["string", "null"],
                "enum": [None, "morning", "afternoon", "night"],
            },
            "amount_mg": {"type": ["integer", "null"]},
            "source": {"type": ["string", "null"]},
            "notes": {"type": ["string", "null"]},
        },
        "required": ["consumed_on", "time_bucket", "amount_mg", "source", "notes"],
    }


def _tool_schema_partial_followup() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workout_type_name": {"type": ["string", "null"]},
            "performed_on": {"type": ["string", "null"]},
            "time_bucket": {"type": ["string", "null"]},
            "notes": {"type": ["string", "null"]},
            "metrics": {"type": "array", "items": _tool_schema_metric_value()},
            "slept_on": {"type": ["string", "null"]},
            "duration_hours": {"type": ["integer", "null"]},
            "duration_minutes": {"type": ["integer", "null"]},
            "quality": {"type": ["integer", "null"]},
            "consumed_on": {"type": ["string", "null"]},
            "amount_mg": {"type": ["integer", "null"]},
            "source": {"type": ["string", "null"]},
        },
        "required": [],
    }


def _build_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "respond_to_user",
            "description": "Send an answer back to the user (for questions / help).",
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send back to the user.",
                    }
                },
                "required": ["message"],
            },
        },
        {
            "type": "function",
            "name": "log_workout",
            "description": "Log a workout entry into the workout tracker.",
            "strict": True,
            "parameters": _tool_schema_partial_workout(),
        },
        {
            "type": "function",
            "name": "log_sleep",
            "description": "Log a sleep entry (duration + optional quality).",
            "strict": True,
            "parameters": _tool_schema_partial_sleep(),
        },
        {
            "type": "function",
            "name": "log_caffeine",
            "description": "Log caffeine intake (amount + timing).",
            "strict": True,
            "parameters": _tool_schema_partial_caffeine(),
        },
        {
            "type": "function",
            "name": "ask_followup",
            "description": "Ask exactly one follow-up question if required information is missing.",
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string"},
                    "context_summary": {"type": ["string", "null"]},
                    "desired_action": {
                        "type": "string",
                        "enum": ["log_workout", "log_sleep", "log_caffeine"],
                    },
                    "partial": _tool_schema_partial_followup(),
                },
                "required": [
                    "question",
                    "context_summary",
                    "desired_action",
                    "partial",
                ],
            },
        },
        {
            "type": "function",
            "name": "ignore_message",
            "description": "Do nothing (message is not intended to control the app).",
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reason": {"type": ["string", "null"]},
                },
                "required": ["reason"],
            },
        },
    ]


def _workout_types_summary(workout_types: list[WorkoutType]) -> str:
    lines: list[str] = []
    for wt in workout_types:
        schema = wt.metric_schema or []
        parts: list[str] = []
        for item in schema:
            key = str(item.get("key") or "").strip()
            t = str(item.get("type") or "string").strip().lower()
            if t in {"text"}:
                t = "string"
            if t in {"number", "int"}:
                t = "integer"
            if t in {"hours", "duration", "time"}:
                # Best-effort normalization for older/hand-entered schemas.
                t = "hours_minutes"
            if t not in {"string", "integer", "hours_minutes"}:
                t = "string"
            if not key:
                continue

            required = bool(item.get("required"))
            default = item.get("default") if "default" in item else None
            has_default = default is not None and default != ""

            extras: list[str] = []
            if required and not has_default:
                extras.append("required")
            if has_default:
                if t == "hours_minutes" and isinstance(default, dict):
                    h = int(default.get("hours") or 0)
                    m = int(default.get("minutes") or 0)
                    extras.append(f"default={h}h {m}m")
                else:
                    extras.append(f"default={default}")

            if extras:
                parts.append(f"{key}:{t} ({', '.join(extras)})")
            else:
                parts.append(f"{key}:{t}")
        if parts:
            lines.append(f"- {wt.name} (metrics: {', '.join(parts)})")
        else:
            lines.append(f"- {wt.name} (no metrics)")
    return "\n".join(lines) if lines else "(no workout types exist yet)"


def _metric_list_to_dict(metrics: list[dict[str, Any]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in metrics or []:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        if item.get("value_hours_minutes") is not None:
            hm = item["value_hours_minutes"]
            out[key] = {
                "hours": int(hm.get("hours") or 0),
                "minutes": int(hm.get("minutes") or 0),
            }
            continue
        if item.get("value_integer") is not None:
            out[key] = int(item["value_integer"])
            continue
        if item.get("value_string") is not None:
            out[key] = str(item["value_string"])
            continue
    return out


def _coerce_metric_value(expected_type: str, value: object) -> object:
    expected_type = expected_type.strip().lower()
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
        if isinstance(value, str):
            s = value.strip().lower()
            if ":" in s:
                parts = s.split(":", 1)
                hours = int(parts[0].strip() or 0)
                minutes = int(parts[1].strip() or 0)
                value = {"hours": hours, "minutes": minutes}
            else:
                raise ValueError("value must be an object with hours/minutes")

        if not isinstance(value, dict):
            raise ValueError("value must be an object with hours/minutes")
        hours = int(value.get("hours") or 0)
        minutes = int(value.get("minutes") or 0)
        if hours < 0 or minutes < 0 or minutes > 59:
            raise ValueError("minutes must be 0-59 and hours >= 0")
        return {"hours": hours, "minutes": minutes}
    raise ValueError("unknown metric type")


def _validate_metrics(
    workout_type: WorkoutType, raw_metrics: dict[str, object]
) -> dict[str, object]:
    schema_items = workout_type.metric_schema or []
    schema_map: dict[str, str] = {}
    required: set[str] = set()
    defaults: dict[str, object] = {}
    for item in schema_items:
        key = str(item.get("key") or "").strip()
        t = str(item.get("type") or "string").strip().lower()
        if t in {"text"}:
            t = "string"
        if t in {"number", "int"}:
            t = "integer"
        if t in {"hours", "duration", "time"}:
            t = "hours_minutes"
        if t not in {"string", "integer", "hours_minutes"}:
            # Keep the system resilient to older/hand-edited metric schemas.
            t = "string"
        if key:
            schema_map[key] = t

            if bool(item.get("required")):
                required.add(key)

            if "default" in item:
                default_value = item.get("default")
                if default_value is not None and default_value != "":
                    defaults[key] = default_value

    validated: dict[str, object] = {}
    # First, fill schema-defined keys (including defaults / required).
    for key, expected in schema_map.items():
        value = (raw_metrics or {}).get(key)
        if value is not None and value != "":
            validated[key] = _coerce_metric_value(expected, value)
            continue

        if key in defaults:
            validated[key] = _coerce_metric_value(expected, defaults[key])
            continue

        if key in required:
            raise ValueError(f"'{key}' is required")

    # Then, allow unknown keys for flexibility.
    for key, value in (raw_metrics or {}).items():
        key = str(key).strip()
        if not key or key in schema_map:
            continue
        if value is None or value == "":
            continue
        if isinstance(value, (str, int)):
            validated[key] = value
        elif isinstance(value, dict) and "hours" in value and "minutes" in value:
            validated[key] = _coerce_metric_value("hours_minutes", value)
        else:
            validated[key] = str(value)

    return validated


def _select_workout_type_by_name(session, name: str) -> WorkoutType | None:
    name = (name or "").strip()
    if not name:
        return None
    return session.scalar(
        select(WorkoutType).where(func.lower(WorkoutType.name) == name.lower())
    )


def _send_ai_ntfy(
    *,
    cfg: NtfyConfig,
    remember_sent_ntfy_id: Callable[[str | None], None] | None,
    title: str,
    message: str,
    tags: list[str],
    priority: int = 3,
) -> str | None:
    msg_id = send_ntfy(
        config=cfg,
        title=title,
        message=message,
        tags=tags,
        priority=priority,
    )
    if remember_sent_ntfy_id is not None:
        remember_sent_ntfy_id(msg_id)
    return msg_id


def handle_ntfy_message(
    *,
    session,
    ntfy_cfg: NtfyConfig,
    ai_cfg: AiConfig,
    topic: str,
    text: str,
    received_event: dict[str, Any],
    remember_sent_ntfy_id: Callable[[str | None], None] | None = None,
) -> dict[str, Any]:
    """Handle an incoming external ntfy message and possibly log a workout.

    Returns a small dict describing what happened (useful for logging).
    """

    if not ai_cfg.enabled:
        return {"handled": False, "reason": "AI disabled"}
    if not ai_cfg.api_key:
        return {"handled": False, "reason": "OPENAI_API_KEY missing"}

    incoming_ntfy_id = str(received_event.get("id") or "").strip() or None
    session.add(
        AiMessage(
            topic=topic,
            role="user",
            content=text,
            ntfy_id=incoming_ntfy_id,
            meta={
                "title": received_event.get("title"),
                "tags": received_event.get("tags"),
            },
        )
    )
    session.commit()

    workout_types = session.scalars(
        select(WorkoutType).order_by(WorkoutType.name.asc())
    ).all()

    pending = session.scalar(
        select(AiPending)
        .where(AiPending.topic == topic)
        .where(AiPending.status == "pending")
        .order_by(AiPending.created_at.desc())
        .limit(1)
    )
    pending_action = None
    if pending is not None and isinstance(pending.context, dict):
        pending_action = pending.context.get("desired_action")

    recent_rows = session.scalars(
        select(AiMessage)
        .where(AiMessage.topic == topic)
        .order_by(AiMessage.created_at.desc())
        .limit(5)
    ).all()
    recent_rows = list(reversed(recent_rows))

    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = dt.datetime.now().astimezone()
    current_bucket = _current_time_bucket(now_local)
    today = now_local.date().isoformat()

    context_lines: list[str] = []
    context_lines.append("Recent conversation (last 5 messages):")
    if recent_rows:
        for msg in recent_rows:
            context_lines.append(f"- {msg.role}: {msg.content}")
    else:
        context_lines.append("- (none)")

    if pending is not None:
        context_lines.append("We previously asked a follow-up question.")
        context_lines.append(f"Question: {pending.question}")
        context_lines.append(f"User answered: {text}")
        if pending_action:
            context_lines.append(f"Pending action: {pending_action}")
        context_lines.append("Prior context JSON:")
        context_lines.append(json.dumps(pending.context or {}, ensure_ascii=False))
    else:
        context_lines.append(f"Incoming message: {text}")

    system_instructions = "\n".join(
        [
            "You are the personal_site automation agent.",
            "Your job: turn the user's ntfy message into exactly one tool call.",
            "If the user is logging a workout, call log_workout.",
            "If the user is logging sleep, call log_sleep.",
            "If the user is logging caffeine, call log_caffeine.",
            "Do your best to infer details from the message. Prefer defaults over follow-ups.",
            "Only call ask_followup if you cannot reasonably infer a required value.",
            "If you call ask_followup, ask exactly one question.",
            "If the user is asking a question (help, status, how-to), call respond_to_user with a concise helpful answer.",
            "If the message is not intended to control the app, call ignore_message.",
            "Use workout_type_name matching one of the existing workout types. If none match, ask a follow-up.",
            "Dates must be ISO YYYY-MM-DD when provided.",
            "time_bucket must be one of: morning, afternoon, night.",
            "If the user does not specify a date, set performed_on to null (the app will use today's date).",
            "If the user does not specify a time bucket, set time_bucket to null (the app will use the current bucket).",
            "Sleep: duration_hours/duration_minutes represent total sleep duration; ask if duration is missing.",
            "Caffeine: amount_mg is required; source is optional.",
        ]
    )

    tool_context = "\n".join(
        [
            f"Current local datetime is {now_local.isoformat()}.",
            f"Current UTC datetime is {now_utc.isoformat()}.",
            f"Today's date is {today}.",
            f"Default time_bucket (if unspecified) is {current_bucket}.",
            "Existing workout types and their metric schemas:",
            _workout_types_summary(workout_types),
            "Sleep logging expects duration_hours/duration_minutes; quality is optional (1-5).",
            "Caffeine logging expects amount_mg; source is optional.",
        ]
    )

    client = OpenAI(api_key=ai_cfg.api_key)
    tools = _build_tools()

    user_input = "\n".join(
        [
            tool_context,
            "---",
            *context_lines,
        ]
    )

    if ai_cfg.debug_log:
        logger.info("ai debug: model=%s", ai_cfg.model)
        logger.info("ai debug: instructions=\n%s", system_instructions)
        logger.info("ai debug: user_input=\n%s", user_input)

    response = client.responses.create(
        model=ai_cfg.model,
        instructions=system_instructions,
        tools=tools,
        input=[
            {
                "role": "user",
                "content": user_input,
            }
        ],
    )

    if ai_cfg.debug_log:
        try:
            logger.info(
                "ai debug: openai_response=\n%s", response.model_dump_json(indent=2)
            )
        except Exception:
            logger.info("ai debug: openai_response_repr=%r", response)

    # Find the first function_call tool output.
    tool_call = None
    for item in response.output:
        if getattr(item, "type", None) == "function_call":
            tool_call = item
            break
        if isinstance(item, dict) and item.get("type") == "function_call":
            tool_call = item
            break

    if tool_call is None:
        if ai_cfg.debug_log:
            logger.info(
                "ai debug: no tool call; output=%r", getattr(response, "output", None)
            )
        return {"handled": False, "reason": "no tool call"}

    name = tool_call.name if hasattr(tool_call, "name") else tool_call.get("name")
    arguments_raw = (
        tool_call.arguments
        if hasattr(tool_call, "arguments")
        else tool_call.get("arguments")
    )
    try:
        args = (
            json.loads(arguments_raw)
            if isinstance(arguments_raw, str)
            else dict(arguments_raw)
        )
    except Exception:
        args = {}

    if ai_cfg.debug_log:
        logger.info("ai debug: tool_name=%s", name)
        logger.info("ai debug: tool_args=%s", json.dumps(args, ensure_ascii=False))

    if name == "ignore_message":
        if pending is not None:
            pending.status = "done"
            session.commit()
        return {"handled": True, "action": "ignored", "reason": args.get("reason")}

    if name == "respond_to_user":
        message = str(args.get("message") or "").strip()
        if not message:
            return {"handled": False, "reason": "empty response message"}

        if pending is not None:
            pending.status = "done"

        try:
            msg_id = _send_ai_ntfy(
                cfg=ntfy_cfg,
                remember_sent_ntfy_id=remember_sent_ntfy_id,
                title="personal_site",
                message=message,
                tags=["ai", "answer"],
                priority=2,
            )
            session.add(
                AiMessage(
                    topic=topic,
                    role="assistant",
                    content=message,
                    ntfy_id=msg_id,
                    meta={"kind": "answer"},
                )
            )
            session.commit()
        except NotificationError as exc:
            session.commit()
            return {"handled": False, "reason": f"failed to send answer: {exc}"}

        return {"handled": True, "action": "respond_to_user"}

    if name == "ask_followup":
        # Never ask more than once per pending thread.
        if pending is not None:
            pending.status = "done"
            session.commit()
            try:
                msg_id = _send_ai_ntfy(
                    cfg=ntfy_cfg,
                    remember_sent_ntfy_id=remember_sent_ntfy_id,
                    title="personal_site",
                    message=(
                        "I still don't have enough info to act on that. "
                        "Please try again with more details (or log it in /workouts/)."
                    ),
                    tags=["ai", "info"],
                    priority=3,
                )
                session.add(
                    AiMessage(
                        topic=topic,
                        role="assistant",
                        content=(
                            "I still don't have enough info to act on that. "
                            "Please try again with more details (or log it in /workouts/)."
                        ),
                        ntfy_id=msg_id,
                        meta={"kind": "info"},
                    )
                )
                session.commit()
            except NotificationError:
                pass
            return {"handled": True, "action": "followup_suppressed"}

        question = str(args.get("question") or "").strip()
        partial = args.get("partial") or {}
        desired_action = args.get("desired_action")
        if not question:
            return {"handled": False, "reason": "empty followup question"}

        if pending is not None:
            pending.question = question
            pending.context = {
                "partial": partial,
                "received_event": received_event,
                "desired_action": desired_action,
            }
        else:
            session.add(
                AiPending(
                    topic=topic,
                    status="pending",
                    question=question,
                    context={
                        "partial": partial,
                        "received_event": received_event,
                        "desired_action": desired_action,
                    },
                )
            )
        session.commit()

        try:
            msg_id = _send_ai_ntfy(
                cfg=ntfy_cfg,
                remember_sent_ntfy_id=remember_sent_ntfy_id,
                title="personal_site question",
                message=question,
                tags=["ai", "question"],
                priority=3,
            )
            session.add(
                AiMessage(
                    topic=topic,
                    role="assistant",
                    content=question,
                    ntfy_id=msg_id,
                    meta={"kind": "question"},
                )
            )
            session.commit()
        except NotificationError as exc:
            return {"handled": False, "reason": f"failed to send followup: {exc}"}

        return {"handled": True, "action": "ask_followup"}

    if name not in {"log_workout", "log_sleep", "log_caffeine"}:
        return {"handled": False, "reason": f"unknown tool: {name}"}

    if name == "log_sleep":
        slept_on_raw = args.get("slept_on")
        duration_hours = args.get("duration_hours")
        duration_minutes = args.get("duration_minutes")
        quality = args.get("quality")
        notes = str(args.get("notes") or "").strip() or None

        if pending is not None and pending_action == "log_sleep":
            partial = (pending.context or {}).get("partial") or {}
            slept_on_raw = slept_on_raw or partial.get("slept_on")
            if duration_hours is None:
                duration_hours = partial.get("duration_hours")
            if duration_minutes is None:
                duration_minutes = partial.get("duration_minutes")
            if quality is None:
                quality = partial.get("quality")
            if notes is None:
                notes = str(partial.get("notes") or "").strip() or None

        if slept_on_raw:
            try:
                slept_on = dt.date.fromisoformat(str(slept_on_raw))
            except ValueError:
                slept_on = dt.date.today()
        else:
            slept_on = dt.date.today()

        hours = int(duration_hours or 0)
        minutes = int(duration_minutes or 0)
        if hours < 0 or minutes < 0 or minutes > 59 or (hours == 0 and minutes == 0):
            if pending is not None and pending_action == "log_sleep":
                pending.status = "done"
                session.commit()
            message = (
                "I need the sleep duration to log that. "
                "Please resend with hours/minutes (e.g. 7h 30m)."
            )
            try:
                msg_id = _send_ai_ntfy(
                    cfg=ntfy_cfg,
                    remember_sent_ntfy_id=remember_sent_ntfy_id,
                    title="personal_site",
                    message=message,
                    tags=["ai", "info"],
                    priority=3,
                )
                session.add(
                    AiMessage(
                        topic=topic,
                        role="assistant",
                        content=message,
                        ntfy_id=msg_id,
                        meta={"kind": "info"},
                    )
                )
                session.commit()
            except NotificationError:
                pass
            return {
                "handled": True,
                "action": "log_sleep",
                "reason": "missing_duration",
            }

        if quality is not None:
            try:
                quality = int(quality)
            except Exception:
                quality = None
            if quality is not None and (quality < 1 or quality > 5):
                quality = None

        session.add(
            SleepEntry(
                slept_on=slept_on,
                duration_minutes=hours * 60 + minutes,
                quality=quality,
                notes=notes,
            )
        )

        if pending is not None and pending_action == "log_sleep":
            pending.status = "done"

        session.commit()

        summary_lines = [
            "Logged sleep",
            f"Date: {slept_on.isoformat()}",
            f"Duration: {hours}h {minutes}m",
        ]
        if quality:
            summary_lines.append(f"Quality: {quality}/5")
        if notes:
            summary_lines.append(f"Notes: {notes}")

        summary = "\n".join(summary_lines)
        try:
            msg_id = _send_ai_ntfy(
                cfg=ntfy_cfg,
                remember_sent_ntfy_id=remember_sent_ntfy_id,
                title="personal_site",
                message=summary,
                tags=["ai", "logged"],
                priority=2,
            )
            session.add(
                AiMessage(
                    topic=topic,
                    role="assistant",
                    content=summary,
                    ntfy_id=msg_id,
                    meta={"kind": "logged"},
                )
            )
            session.commit()
        except NotificationError:
            pass

        return {"handled": True, "action": "log_sleep", "summary": summary}

    if name == "log_caffeine":
        consumed_on_raw = args.get("consumed_on")
        time_bucket = (
            (args.get("time_bucket") or "").strip().lower()
            if args.get("time_bucket")
            else None
        )
        amount_mg = args.get("amount_mg")
        source = str(args.get("source") or "").strip() or None
        notes = str(args.get("notes") or "").strip() or None

        if pending is not None and pending_action == "log_caffeine":
            partial = (pending.context or {}).get("partial") or {}
            consumed_on_raw = consumed_on_raw or partial.get("consumed_on")
            if time_bucket is None:
                time_bucket = partial.get("time_bucket") or None
            if amount_mg is None:
                amount_mg = partial.get("amount_mg")
            if source is None:
                source = str(partial.get("source") or "").strip() or None
            if notes is None:
                notes = str(partial.get("notes") or "").strip() or None

        if amount_mg is None:
            if pending is not None and pending_action == "log_caffeine":
                pending.status = "done"
                session.commit()
            message = "I need the caffeine amount (mg) to log that. Please resend with the amount."
            try:
                msg_id = _send_ai_ntfy(
                    cfg=ntfy_cfg,
                    remember_sent_ntfy_id=remember_sent_ntfy_id,
                    title="personal_site",
                    message=message,
                    tags=["ai", "info"],
                    priority=3,
                )
                session.add(
                    AiMessage(
                        topic=topic,
                        role="assistant",
                        content=message,
                        ntfy_id=msg_id,
                        meta={"kind": "info"},
                    )
                )
                session.commit()
            except NotificationError:
                pass
            return {
                "handled": True,
                "action": "log_caffeine",
                "reason": "missing_amount",
            }

        try:
            amount_mg = int(amount_mg)
        except Exception:
            amount_mg = None

        if amount_mg is None or amount_mg <= 0:
            if pending is not None and pending_action == "log_caffeine":
                pending.status = "done"
                session.commit()
            message = "Caffeine amount must be a positive integer (mg). Please resend with the amount."
            try:
                msg_id = _send_ai_ntfy(
                    cfg=ntfy_cfg,
                    remember_sent_ntfy_id=remember_sent_ntfy_id,
                    title="personal_site",
                    message=message,
                    tags=["ai", "info"],
                    priority=3,
                )
                session.add(
                    AiMessage(
                        topic=topic,
                        role="assistant",
                        content=message,
                        ntfy_id=msg_id,
                        meta={"kind": "info"},
                    )
                )
                session.commit()
            except NotificationError:
                pass
            return {
                "handled": True,
                "action": "log_caffeine",
                "reason": "invalid_amount",
            }

        if consumed_on_raw:
            try:
                consumed_on = dt.date.fromisoformat(str(consumed_on_raw))
            except ValueError:
                consumed_on = dt.date.today()
        else:
            consumed_on = dt.date.today()

        if not time_bucket:
            time_bucket = _current_time_bucket(dt.datetime.now())
        if time_bucket not in ALLOWED_TIME_BUCKETS:
            time_bucket = _current_time_bucket(dt.datetime.now())

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

        if pending is not None and pending_action == "log_caffeine":
            pending.status = "done"

        session.commit()

        summary_lines = [
            "Logged caffeine",
            f"Amount: {amount_mg} mg",
            f"Date: {consumed_on.isoformat()}",
            f"Time bucket: {time_bucket}",
        ]
        if source:
            summary_lines.append(f"Source: {source}")
        if notes:
            summary_lines.append(f"Notes: {notes}")

        summary = "\n".join(summary_lines)
        try:
            msg_id = _send_ai_ntfy(
                cfg=ntfy_cfg,
                remember_sent_ntfy_id=remember_sent_ntfy_id,
                title="personal_site",
                message=summary,
                tags=["ai", "logged"],
                priority=2,
            )
            session.add(
                AiMessage(
                    topic=topic,
                    role="assistant",
                    content=summary,
                    ntfy_id=msg_id,
                    meta={"kind": "logged"},
                )
            )
            session.commit()
        except NotificationError:
            pass

        return {"handled": True, "action": "log_caffeine", "summary": summary}

    workout_type_name = str(args.get("workout_type_name") or "").strip()
    performed_on_raw = args.get("performed_on")
    time_bucket = (
        (args.get("time_bucket") or "").strip().lower()
        if args.get("time_bucket")
        else None
    )
    notes = str(args.get("notes") or "").strip() or None

    if pending is not None and pending_action == "log_workout":
        # Merge in pending partial if present
        partial = (pending.context or {}).get("partial") or {}
        workout_type_name = (
            workout_type_name or str(partial.get("workout_type_name") or "").strip()
        )
        performed_on_raw = performed_on_raw or partial.get("performed_on")
        time_bucket = time_bucket or (partial.get("time_bucket") or None)
        if notes is None:
            notes = str(partial.get("notes") or "").strip() or None
        # Merge metrics lists
        if not args.get("metrics") and partial.get("metrics"):
            args["metrics"] = partial.get("metrics")

    if not workout_type_name and len(workout_types) == 1:
        workout_type_name = workout_types[0].name

    if not workout_type_name:
        if pending is not None:
            pending.status = "done"
            session.commit()
            try:
                msg_id = _send_ai_ntfy(
                    cfg=ntfy_cfg,
                    remember_sent_ntfy_id=remember_sent_ntfy_id,
                    title="personal_site",
                    message=(
                        "I couldn't determine the workout type from your reply. "
                        "Please re-send with the workout type name, or log it in /workouts/."
                    ),
                    tags=["ai", "info"],
                    priority=3,
                )
                session.add(
                    AiMessage(
                        topic=topic,
                        role="assistant",
                        content=(
                            "I couldn't determine the workout type from your reply. "
                            "Please re-send with the workout type name, or log it in /workouts/."
                        ),
                        ntfy_id=msg_id,
                        meta={"kind": "info"},
                    )
                )
                session.commit()
            except NotificationError:
                pass
            return {"handled": True, "action": "followup_suppressed"}

        if not workout_types:
            try:
                msg_id = _send_ai_ntfy(
                    cfg=ntfy_cfg,
                    remember_sent_ntfy_id=remember_sent_ntfy_id,
                    title="personal_site",
                    message=(
                        "No workout types exist yet. Create one at /workouts/types/new, "
                        "then message me again."
                    ),
                    tags=["ai", "info"],
                    priority=3,
                )
                session.add(
                    AiMessage(
                        topic=topic,
                        role="assistant",
                        content=(
                            "No workout types exist yet. Create one at /workouts/types/new, "
                            "then message me again."
                        ),
                        ntfy_id=msg_id,
                        meta={"kind": "info"},
                    )
                )
                session.commit()
            except NotificationError:
                pass
            return {"handled": True, "action": "no_workout_types"}

        type_names = ", ".join([wt.name for wt in workout_types])
        question = f"Which workout type was that? Available: {type_names}"
        session.add(
            AiPending(
                topic=topic,
                status="pending",
                question=question,
                context={
                    "partial": args,
                    "received_event": received_event,
                    "desired_action": "log_workout",
                },
            )
        )
        session.commit()
        msg_id = _send_ai_ntfy(
            cfg=ntfy_cfg,
            remember_sent_ntfy_id=remember_sent_ntfy_id,
            title="personal_site question",
            message=question,
            tags=["ai", "question"],
            priority=3,
        )
        session.add(
            AiMessage(
                topic=topic,
                role="assistant",
                content=question,
                ntfy_id=msg_id,
                meta={"kind": "question"},
            )
        )
        session.commit()
        return {"handled": True, "action": "ask_followup"}

    workout_type = _select_workout_type_by_name(session, workout_type_name)
    if workout_type is None:
        if len(workout_types) == 1:
            workout_type = workout_types[0]
        else:
            if pending is not None:
                pending.status = "done"
                session.commit()
                try:
                    msg_id = _send_ai_ntfy(
                        cfg=ntfy_cfg,
                        remember_sent_ntfy_id=remember_sent_ntfy_id,
                        title="personal_site",
                        message=(
                            f"I don't recognize workout type '{workout_type_name}'. "
                            "Please re-send with an exact workout type name, or log it in /workouts/."
                        ),
                        tags=["ai", "info"],
                        priority=3,
                    )
                    session.add(
                        AiMessage(
                            topic=topic,
                            role="assistant",
                            content=(
                                f"I don't recognize workout type '{workout_type_name}'. "
                                "Please re-send with an exact workout type name, or log it in /workouts/."
                            ),
                            ntfy_id=msg_id,
                            meta={"kind": "info"},
                        )
                    )
                    session.commit()
                except NotificationError:
                    pass
                return {"handled": True, "action": "followup_suppressed"}

        type_names = ", ".join([wt.name for wt in workout_types])
        question = (
            f"I don't recognize workout type '{workout_type_name}'. Which type should I use? Available: {type_names}"
            if type_names
            else f"I don't recognize workout type '{workout_type_name}'. Create a workout type at /workouts/types/new, then message me again."
        )
        session.add(
            AiPending(
                topic=topic,
                status="pending",
                question=question,
                context={
                    "partial": args,
                    "received_event": received_event,
                    "desired_action": "log_workout",
                },
            )
        )
        session.commit()
        msg_id = _send_ai_ntfy(
            cfg=ntfy_cfg,
            remember_sent_ntfy_id=remember_sent_ntfy_id,
            title="personal_site question",
            message=question,
            tags=["ai", "question"],
            priority=3,
        )
        session.add(
            AiMessage(
                topic=topic,
                role="assistant",
                content=question,
                ntfy_id=msg_id,
                meta={"kind": "question"},
            )
        )
        session.commit()
        return {"handled": True, "action": "ask_followup"}

    if performed_on_raw:
        try:
            performed_on = dt.date.fromisoformat(str(performed_on_raw))
        except ValueError:
            performed_on = dt.date.today()
    else:
        performed_on = dt.date.today()

    if not time_bucket:
        time_bucket = _current_time_bucket(dt.datetime.now())
    if time_bucket not in ALLOWED_TIME_BUCKETS:
        time_bucket = _current_time_bucket(dt.datetime.now())

    performed_at = dt.datetime.combine(
        performed_on, _bucket_to_time(time_bucket)
    ).replace(tzinfo=dt.timezone.utc)

    raw_metrics = _metric_list_to_dict(args.get("metrics") or [])
    try:
        metrics = _validate_metrics(workout_type, raw_metrics)
    except Exception as exc:
        if pending is not None:
            pending.status = "done"
            session.commit()
            try:
                msg_id = _send_ai_ntfy(
                    cfg=ntfy_cfg,
                    remember_sent_ntfy_id=remember_sent_ntfy_id,
                    title="personal_site",
                    message=(
                        f"I couldn't validate metrics for {workout_type.name}: {exc}. "
                        "Please re-send with clearer values, or log it in /workouts/."
                    ),
                    tags=["ai", "info"],
                    priority=3,
                )
                session.add(
                    AiMessage(
                        topic=topic,
                        role="assistant",
                        content=(
                            f"I couldn't validate metrics for {workout_type.name}: {exc}. "
                            "Please re-send with clearer values, or log it in /workouts/."
                        ),
                        ntfy_id=msg_id,
                        meta={"kind": "info"},
                    )
                )
                session.commit()
            except NotificationError:
                pass
            return {"handled": True, "action": "followup_suppressed"}

        question = f"I couldn't validate metrics for {workout_type.name}: {exc}. Can you rephrase with values?"
        session.add(
            AiPending(
                topic=topic,
                status="pending",
                question=question,
                context={
                    "partial": args,
                    "received_event": received_event,
                    "desired_action": "log_workout",
                },
            )
        )
        session.commit()
        msg_id = _send_ai_ntfy(
            cfg=ntfy_cfg,
            remember_sent_ntfy_id=remember_sent_ntfy_id,
            title="personal_site question",
            message=question,
            tags=["ai", "question"],
            priority=3,
        )
        session.add(
            AiMessage(
                topic=topic,
                role="assistant",
                content=question,
                ntfy_id=msg_id,
                meta={"kind": "question"},
            )
        )
        session.commit()
        return {"handled": True, "action": "ask_followup"}

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

    if pending is not None:
        pending.status = "done"

    session.commit()

    metric_lines: list[str] = []
    for k, v in (metrics or {}).items():
        suffix = ""
        raw_value = raw_metrics.get(k)
        if raw_value is None or raw_value == "":
            # _validate_metrics may apply defaults; call that out.
            suffix = " (default)"

        if isinstance(v, dict) and "hours" in v and "minutes" in v:
            h = int(v.get("hours") or 0)
            m = int(v.get("minutes") or 0)
            metric_lines.append(f"- {k}: {h}h {m}m{suffix}")
        else:
            metric_lines.append(f"- {k}: {v}{suffix}")

    summary_lines: list[str] = [
        "Logged workout",
        f"Type: {workout_type.name}",
        f"Date: {performed_on.isoformat()}",
        f"Time bucket: {time_bucket}",
    ]

    if notes:
        summary_lines.append(f"Notes: {notes}")

    if metric_lines:
        summary_lines.append("Metrics:")
        summary_lines.extend(metric_lines)

    summary = "\n".join(summary_lines)
    try:
        msg_id = _send_ai_ntfy(
            cfg=ntfy_cfg,
            remember_sent_ntfy_id=remember_sent_ntfy_id,
            title="personal_site",
            message=summary,
            tags=["ai", "logged"],
            priority=2,
        )
        session.add(
            AiMessage(
                topic=topic,
                role="assistant",
                content=summary,
                ntfy_id=msg_id,
                meta={"kind": "logged"},
            )
        )
        session.commit()
    except NotificationError:
        pass

    return {"handled": True, "action": "log_workout", "summary": summary}
