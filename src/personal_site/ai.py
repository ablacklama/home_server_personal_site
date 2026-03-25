from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from anthropic import Anthropic
from sqlalchemy import func, select

from .ai_models import AiMessage, AiPending
from .notify import NotificationError, NtfyConfig, send_ntfy
from .workouts import ALLOWED_TIME_BUCKETS, _bucket_to_time, _current_time_bucket
from .caffeine_models import CaffeineEntry
from .nutrition_models import (
    Ingredient,
    Meal,
    MealIngredient,
    NutritionLog,
    NutritionLogItem,
)
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


def _build_tools(*, include_web_search: bool = False) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if include_web_search:
        tools.append(
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }
        )
    tools.extend(
        [
            {
                "name": "respond_to_user",
                "description": "Send an answer back to the user (for questions / help).",
                "input_schema": {
                    "type": "object",
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
                "name": "log_workout",
                "description": "Log a workout entry into the workout tracker.",
                "input_schema": _tool_schema_partial_workout(),
            },
            {
                "name": "log_sleep",
                "description": "Log a sleep entry (duration + optional quality).",
                "input_schema": _tool_schema_partial_sleep(),
            },
            {
                "name": "log_caffeine",
                "description": "Log caffeine intake (amount + timing).",
                "input_schema": _tool_schema_partial_caffeine(),
            },
            {
                "name": "ask_followup",
                "description": "Ask exactly one follow-up question if required information is missing.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "context_summary": {"type": ["string", "null"]},
                        "desired_action": {
                            "type": "string",
                            "enum": [
                                "log_workout",
                                "log_sleep",
                                "log_caffeine",
                                "create_meal",
                                "log_nutrition",
                            ],
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
                "name": "create_meal",
                "description": (
                    "Create a saved meal with its ingredients. "
                    "For each ingredient, if it already exists (by name) it will be reused; "
                    "otherwise a new ingredient is created with the provided nutritional info. "
                    "Use your best estimates for calories/protein/carbs/fat per serving."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "meal_name": {
                            "type": "string",
                            "description": "Name for the meal.",
                        },
                        "notes": {"type": ["string", "null"]},
                        "ingredients": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {
                                        "type": "string",
                                        "description": "Ingredient name.",
                                    },
                                    "servings": {
                                        "type": "number",
                                        "description": "Number of servings (default 1).",
                                    },
                                    "serving_size": {
                                        "type": ["string", "null"],
                                        "description": "e.g. '1 patty', '1 tablespoon', '1 muffin'.",
                                    },
                                    "calories": {
                                        "type": ["number", "null"],
                                        "description": "Calories per serving.",
                                    },
                                    "protein_g": {"type": ["number", "null"]},
                                    "carbs_g": {"type": ["number", "null"]},
                                    "fat_g": {"type": ["number", "null"]},
                                    "sugar_g": {"type": ["number", "null"]},
                                },
                                "required": ["name", "servings"],
                            },
                        },
                    },
                    "required": ["meal_name", "ingredients"],
                },
            },
            {
                "name": "log_nutrition",
                "description": (
                    "Log a nutrition entry. Either reference an existing meal by name, "
                    "or provide ad-hoc ingredients. Optionally also logs the meal."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "logged_on": {
                            "type": ["string", "null"],
                            "description": "ISO date YYYY-MM-DD, or null for today.",
                        },
                        "time_bucket": {
                            "type": ["string", "null"],
                            "enum": [None, "morning", "afternoon", "night"],
                        },
                        "meal_name": {
                            "type": ["string", "null"],
                            "description": "Name of an existing saved meal to log.",
                        },
                        "ingredients": {
                            "type": ["array", "null"],
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "servings": {"type": "number"},
                                    "serving_size": {
                                        "type": ["string", "null"],
                                        "description": "e.g. '1 piece', '1 cup'. Only needed for new ingredients.",
                                    },
                                    "calories": {"type": ["number", "null"]},
                                    "protein_g": {"type": ["number", "null"]},
                                    "carbs_g": {"type": ["number", "null"]},
                                    "fat_g": {"type": ["number", "null"]},
                                    "sugar_g": {"type": ["number", "null"]},
                                },
                                "required": ["name", "servings"],
                            },
                            "description": "Ingredients to log. If an ingredient doesn't exist yet, include its nutrition info to create it.",
                        },
                        "notes": {"type": ["string", "null"]},
                    },
                    "required": ["logged_on", "time_bucket"],
                },
            },
            {
                "name": "search_ingredients",
                "description": (
                    "Search saved ingredients by keyword, or list all if no keyword given. "
                    "You MUST call this BEFORE logging nutrition to check if ingredients already exist. "
                    "If an ingredient is found here, use it by name — do NOT web search for it. "
                    "Only web search for ingredients NOT found here."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": ["string", "null"],
                            "description": "Search term (case-insensitive partial match). Null/empty returns all.",
                        },
                    },
                    "required": ["keyword"],
                },
            },
            {
                "name": "list_conversation_entries",
                "description": (
                    "List all entries (nutrition, sleep, workout, caffeine) that were "
                    "logged during this conversation. Returns entry IDs and types so you "
                    "can edit or delete them."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "edit_entry",
                "description": (
                    "Edit an entry that was logged in this conversation. "
                    "First call list_conversation_entries to get the entry_id and entry_type. "
                    "Only include fields you want to change in updates."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "The ID of the entry to edit.",
                        },
                        "entry_type": {
                            "type": "string",
                            "enum": [
                                "nutrition",
                                "sleep",
                                "workout",
                                "caffeine",
                            ],
                        },
                        "updates": {
                            "type": "object",
                            "description": (
                                "Fields to update. For nutrition: logged_on, time_bucket, notes, "
                                "ingredients (array of {name, servings, serving_size, calories, "
                                "protein_g, carbs_g, fat_g, sugar_g} — replaces all items). "
                                "For sleep: slept_on, duration_hours, duration_minutes, quality, notes. "
                                "For workout: performed_on, time_bucket, notes. "
                                "For caffeine: consumed_on, time_bucket, amount_mg, source, notes."
                            ),
                        },
                    },
                    "required": ["entry_id", "entry_type", "updates"],
                },
            },
            {
                "name": "delete_entry",
                "description": (
                    "Delete an entry that was logged in this conversation. "
                    "First call list_conversation_entries to get the entry_id and entry_type."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {
                            "type": "string",
                            "description": "The ID of the entry to delete.",
                        },
                        "entry_type": {
                            "type": "string",
                            "enum": [
                                "nutrition",
                                "sleep",
                                "workout",
                                "caffeine",
                                "meal",
                            ],
                        },
                    },
                    "required": ["entry_id", "entry_type"],
                },
            },
            {
                "name": "ignore_message",
                "description": "Do nothing (message is not intended to control the app).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": ["string", "null"]},
                    },
                    "required": ["reason"],
                },
            },
        ]
    )
    return tools


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
        return {"handled": False, "reason": "ANTHROPIC_API_KEY missing"}

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

    client = Anthropic(api_key=ai_cfg.api_key)
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
        logger.info("ai debug: system=\n%s", system_instructions)
        logger.info("ai debug: user_input=\n%s", user_input)

    response = client.messages.create(
        model=ai_cfg.model,
        max_tokens=1024,
        system=system_instructions,
        tools=tools,
        messages=[
            {
                "role": "user",
                "content": user_input,
            }
        ],
    )

    if ai_cfg.debug_log:
        try:
            logger.info(
                "ai debug: anthropic_response=\n%s",
                response.model_dump_json(indent=2),
            )
        except Exception:
            logger.info("ai debug: anthropic_response_repr=%r", response)

    # Find the first tool_use content block.
    tool_call = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            tool_call = block
            break

    if tool_call is None:
        if ai_cfg.debug_log:
            logger.info(
                "ai debug: no tool call; content=%r",
                getattr(response, "content", None),
            )
        return {"handled": False, "reason": "no tool call"}

    name = tool_call.name
    args = tool_call.input if isinstance(tool_call.input, dict) else {}

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


def handle_chat_message(
    *,
    session,
    ai_cfg: AiConfig,
    conversation_id: str,
) -> dict[str, Any]:
    """Process the latest user message in a chat conversation via Claude.

    Loads conversation history, calls Claude with tools, executes the tool,
    and stores the assistant response. Used by the web chat interface.
    """
    if not ai_cfg.enabled:
        session.add(
            AiMessage(
                conversation_id=conversation_id,
                role="assistant",
                content="AI is not enabled. Set AI_ENABLED=true and provide an ANTHROPIC_API_KEY.",
                meta={"kind": "info"},
            )
        )
        session.commit()
        return {"handled": False, "reason": "AI disabled"}

    if not ai_cfg.api_key:
        session.add(
            AiMessage(
                conversation_id=conversation_id,
                role="assistant",
                content="ANTHROPIC_API_KEY is not configured.",
                meta={"kind": "info"},
            )
        )
        session.commit()
        return {"handled": False, "reason": "ANTHROPIC_API_KEY missing"}

    # Load recent messages for context
    recent_rows = session.scalars(
        select(AiMessage)
        .where(AiMessage.conversation_id == conversation_id)
        .order_by(AiMessage.created_at.desc())
        .limit(20)
    ).all()
    recent_rows = list(reversed(recent_rows))

    # Build Claude messages from conversation history — skip status/info/error
    claude_messages: list[dict[str, str]] = []
    for msg in recent_rows:
        kind = (msg.meta or {}).get("kind") if msg.meta else None
        if kind in ("status", "info", "error"):
            continue
        claude_messages.append({"role": msg.role, "content": msg.content})

    workout_types = session.scalars(
        select(WorkoutType).order_by(WorkoutType.name.asc())
    ).all()

    now_local = dt.datetime.now().astimezone()
    current_bucket = _current_time_bucket(now_local)
    today = now_local.date().isoformat()

    # Gather existing meals for context
    existing_meals = session.scalars(select(Meal).order_by(Meal.name.asc())).all()
    meals_summary = (
        ", ".join(m.name for m in existing_meals) if existing_meals else "(none)"
    )

    system_instructions = "\n".join(
        [
            "You are a personal health tracking assistant on a web app.",
            "You help the user log workouts, sleep, caffeine, and nutrition/meals.",
            "When the user wants to log something, use the appropriate tool.",
            "If the user is asking a question or chatting, call respond_to_user.",
            "If you need more info to complete a log, ask in your respond_to_user message.",
            "Be concise and helpful.",
            "",
            f"Current local datetime: {now_local.isoformat()}",
            f"Today's date: {today}",
            f"Current time bucket: {current_bucket}",
            "",
            "Existing workout types:",
            _workout_types_summary(workout_types),
            "",
            f"Existing saved meals: {meals_summary}",
            "",
            "Rules:",
            "- Dates must be ISO YYYY-MM-DD.",
            "- time_bucket: morning, afternoon, or night.",
            "- If date not specified, set to null (app uses today).",
            "- If time_bucket not specified, set to null (app uses current).",
            "- Sleep: duration_hours/duration_minutes for total sleep.",
            "- Caffeine: amount_mg required, source optional.",
            "- Nutrition: use log_nutrition to log what the user ate. It creates new ingredients automatically.",
            "  Include full nutrition info (calories, protein, carbs, fat, sugar) for any new ingredients.",
            "  BEFORE logging, ALWAYS call search_ingredients first to check if ingredients already exist.",
            "  If they exist, reuse them by exact name — do NOT create duplicates or web search for them.",
            "  ONLY use web_search for ingredients that were NOT found by search_ingredients.",
            "  For branded products (e.g. 'Dove chocolate'), search for the exact product's nutrition label.",
            "- ONLY use create_meal when the user explicitly asks to save/create a reusable meal template.",
            "  Saved meals are for recurring meals (e.g. 'my breakfast sandwich'), NOT for one-off logging.",
            "  If the user just says they ate something, use log_nutrition directly — do NOT create a meal.",
            "- Editing/deleting: if the user wants to change or remove something they logged in this conversation,",
            "  call list_conversation_entries to find the entry_id, then use edit_entry or delete_entry.",
        ]
    )

    tools = _build_tools(include_web_search=True)

    client = Anthropic(api_key=ai_cfg.api_key)

    if ai_cfg.debug_log:
        logger.info(
            "chat debug: model=%s conversation=%s", ai_cfg.model, conversation_id
        )

    # -- helpers for live status updates visible via poll --
    _status_ids: list[str] = []

    def _set_status(text: str) -> None:
        """Write a temporary status message that the poll endpoint picks up."""
        _clear_status()
        msg = AiMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=text,
            meta={"kind": "status"},
        )
        session.add(msg)
        session.commit()
        _status_ids.append(msg.id)

    def _clear_status() -> None:
        """Remove any outstanding status messages."""
        for sid in _status_ids:
            old = session.get(AiMessage, sid)
            if old:
                session.delete(old)
        _status_ids.clear()
        session.commit()

    _TOOL_LABELS = {
        "web_search": "Searching the web…",
        "search_ingredients": "Checking ingredients…",
        "create_meal": "Creating meal…",
        "log_nutrition": "Logging nutrition…",
        "log_workout": "Logging workout…",
        "log_sleep": "Logging sleep…",
        "log_caffeine": "Logging caffeine…",
        "list_conversation_entries": "Looking up entries…",
        "edit_entry": "Editing entry…",
        "delete_entry": "Deleting entry…",
        "respond_to_user": "Thinking…",
        "ask_followup": "Thinking…",
    }

    # Tool loop: Claude may call web_search (server-side) then custom tools
    max_iterations = 8
    last_action = "text_response"

    _set_status("Thinking…")

    for _iteration in range(max_iterations):
        response = client.messages.create(
            model=ai_cfg.model,
            max_tokens=4096,
            system=system_instructions,
            tools=tools,
            messages=claude_messages,
        )

        if ai_cfg.debug_log:
            try:
                logger.info(
                    "chat debug: iter=%d response=\n%s",
                    _iteration,
                    response.model_dump_json(indent=2),
                )
            except Exception:
                logger.info("chat debug: response_repr=%r", response)

        # If the model is done, extract final text
        if response.stop_reason == "end_turn":
            _clear_status()
            text_parts: list[str] = []
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    text_parts.append(block.text)
            reply = "\n".join(text_parts).strip()
            logger.info(
                "chat end_turn: iter=%d last_action=%s reply=%s",
                _iteration,
                last_action,
                reply[:200] if reply else "(empty)",
            )
            if reply:
                session.add(
                    AiMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=reply,
                        meta={"kind": "text"},
                    )
                )
                session.commit()
            return {"handled": True, "action": last_action}

        # Model wants to use tools — find all tool_use blocks
        tool_calls = [
            block
            for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ]

        if not tool_calls:
            _clear_status()
            # No tool calls and not end_turn — extract any text
            text_parts = []
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    text_parts.append(block.text)
            reply = "\n".join(text_parts).strip()
            if reply:
                session.add(
                    AiMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=reply,
                        meta={"kind": "text"},
                    )
                )
                session.commit()
            return {"handled": True, "action": last_action}

        # Append assistant message (with all content blocks) to conversation
        claude_messages.append({"role": "assistant", "content": response.content})

        # Execute each tool and build results
        tool_results: list[dict[str, Any]] = []
        for tc in tool_calls:
            tc_name = tc.name
            tc_args = tc.input if isinstance(tc.input, dict) else {}

            # Show live status for this tool
            label = _TOOL_LABELS.get(tc_name, f"Running {tc_name}…")
            _set_status(label)

            logger.info(
                "chat tool call: %s args=%s",
                tc_name,
                json.dumps(tc_args, ensure_ascii=False),
            )

            result = _execute_chat_tool(
                session=session,
                name=tc_name,
                args=tc_args,
                conversation_id=conversation_id,
            )
            last_action = tc_name

            logger.info("chat tool result: %s → %s", tc_name, result.text[:200])

            # Save a visible confirmation for actions that modify data
            _CONFIRM_TOOLS = {
                "log_nutrition",
                "log_workout",
                "log_sleep",
                "log_caffeine",
                "create_meal",
                "edit_entry",
                "delete_entry",
            }
            if tc_name in _CONFIRM_TOOLS and not result.text.startswith(
                ("I need", "Unknown", "entry_id")
            ):
                meta: dict[str, Any] = {"kind": "info"}
                if result.entry_id:
                    meta["entry_id"] = result.entry_id
                if result.entry_type:
                    meta["entry_type"] = result.entry_type
                session.add(
                    AiMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=result.text,
                        meta=meta,
                    )
                )
                session.commit()

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result.text,
                }
            )

        # Update status before next API call
        _set_status("Thinking…")

        # Append tool results as a user message and loop
        claude_messages.append({"role": "user", "content": tool_results})

    # If we exhausted iterations, save whatever we have
    _clear_status()
    session.add(
        AiMessage(
            conversation_id=conversation_id,
            role="assistant",
            content="Done — processed your request.",
            meta={"kind": "tool_result", "tool": last_action},
        )
    )
    session.commit()
    return {"handled": True, "action": last_action}


@dataclass
class ToolResult:
    text: str
    entry_id: str | None = None
    entry_type: str | None = None


def _execute_chat_tool(
    *,
    session,
    name: str,
    args: dict[str, Any],
    conversation_id: str | None = None,
) -> ToolResult:
    """Execute a tool call from the chat and return a structured result."""
    if name == "respond_to_user":
        return ToolResult(str(args.get("message") or "").strip() or "OK")

    if name == "ignore_message":
        return ToolResult("")

    if name == "search_ingredients":
        keyword = (str(args.get("keyword") or "")).strip().lower()
        query = select(Ingredient).order_by(Ingredient.name.asc())
        if keyword:
            query = query.where(Ingredient.name.ilike(f"%{keyword}%"))
        results = list(session.scalars(query).all())
        if not results:
            return ToolResult(
                "No ingredients found."
                + (f" No match for '{keyword}'." if keyword else "")
            )
        lines = []
        for ing in results:
            parts = [ing.name]
            if ing.serving_size:
                parts.append(f"(serving: {ing.serving_size})")
            info = []
            if ing.calories is not None:
                info.append(f"{ing.calories:.0f} cal")
            if ing.protein_g is not None:
                info.append(f"{ing.protein_g:.1f}g protein")
            if ing.carbs_g is not None:
                info.append(f"{ing.carbs_g:.1f}g carbs")
            if ing.fat_g is not None:
                info.append(f"{ing.fat_g:.1f}g fat")
            if ing.sugar_g is not None:
                info.append(f"{ing.sugar_g:.1f}g sugar")
            if ing.caffeine_mg is not None:
                info.append(f"{ing.caffeine_mg:.0f}mg caffeine")
            if info:
                parts.append("— " + ", ".join(info))
            lines.append(" ".join(parts))
        return ToolResult(f"Found {len(results)} ingredient(s):\n" + "\n".join(lines))

    if name == "log_sleep":
        slept_on_raw = args.get("slept_on")
        duration_hours = args.get("duration_hours")
        duration_minutes = args.get("duration_minutes")
        quality = args.get("quality")
        notes = str(args.get("notes") or "").strip() or None

        if slept_on_raw:
            try:
                slept_on = dt.date.fromisoformat(str(slept_on_raw))
            except ValueError:
                slept_on = dt.date.today()
        else:
            slept_on = dt.date.today()

        hours = int(duration_hours or 0)
        minutes = int(duration_minutes or 0)
        if hours == 0 and minutes == 0:
            return ToolResult(
                "I need the sleep duration to log that. How long did you sleep?"
            )

        if quality is not None:
            try:
                quality = int(quality)
            except Exception:
                quality = None
            if quality is not None and (quality < 1 or quality > 5):
                quality = None

        entry = SleepEntry(
            slept_on=slept_on,
            duration_minutes=hours * 60 + minutes,
            quality=quality,
            notes=notes,
        )
        session.add(entry)
        session.commit()

        parts = [f"Logged sleep: {hours}h {minutes}m on {slept_on.isoformat()}"]
        if quality:
            parts.append(f"Quality: {quality}/5")
        return ToolResult(". ".join(parts), entry_id=entry.id, entry_type="sleep")

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

        if amount_mg is None:
            return ToolResult("I need the caffeine amount (mg) to log that.")

        try:
            amount_mg = int(amount_mg)
        except Exception:
            return ToolResult("Caffeine amount must be a number (mg).")

        if amount_mg <= 0:
            return ToolResult("Caffeine amount must be positive.")

        if consumed_on_raw:
            try:
                consumed_on = dt.date.fromisoformat(str(consumed_on_raw))
            except ValueError:
                consumed_on = dt.date.today()
        else:
            consumed_on = dt.date.today()

        if not time_bucket or time_bucket not in ALLOWED_TIME_BUCKETS:
            time_bucket = _current_time_bucket(dt.datetime.now())

        consumed_at = dt.datetime.combine(
            consumed_on, _bucket_to_time(time_bucket)
        ).replace(tzinfo=dt.timezone.utc)

        entry = CaffeineEntry(
            consumed_at=consumed_at,
            consumed_on=consumed_on,
            time_bucket=time_bucket,
            amount_mg=amount_mg,
            source=source,
            notes=notes,
        )
        session.add(entry)
        session.commit()

        parts = [f"Logged caffeine: {amount_mg}mg on {consumed_on.isoformat()}"]
        if source:
            parts.append(f"Source: {source}")
        return ToolResult(". ".join(parts), entry_id=entry.id, entry_type="caffeine")

    if name == "log_workout":
        workout_type_name = str(args.get("workout_type_name") or "").strip()
        performed_on_raw = args.get("performed_on")
        time_bucket = (
            (args.get("time_bucket") or "").strip().lower()
            if args.get("time_bucket")
            else None
        )
        notes = str(args.get("notes") or "").strip() or None

        workout_types = session.scalars(
            select(WorkoutType).order_by(WorkoutType.name.asc())
        ).all()

        if not workout_type_name and len(workout_types) == 1:
            workout_type_name = workout_types[0].name

        if not workout_type_name:
            type_names = ", ".join([wt.name for wt in workout_types])
            return ToolResult(
                f"Which workout type? Available: {type_names}"
                if type_names
                else "No workout types exist yet. Create one in /workouts/types/new."
            )

        workout_type = _select_workout_type_by_name(session, workout_type_name)
        if workout_type is None and len(workout_types) == 1:
            workout_type = workout_types[0]
        if workout_type is None:
            type_names = ", ".join([wt.name for wt in workout_types])
            return ToolResult(
                f"I don't recognize '{workout_type_name}'. Available: {type_names}"
            )

        if performed_on_raw:
            try:
                performed_on = dt.date.fromisoformat(str(performed_on_raw))
            except ValueError:
                performed_on = dt.date.today()
        else:
            performed_on = dt.date.today()

        if not time_bucket or time_bucket not in ALLOWED_TIME_BUCKETS:
            time_bucket = _current_time_bucket(dt.datetime.now())

        performed_at = dt.datetime.combine(
            performed_on, _bucket_to_time(time_bucket)
        ).replace(tzinfo=dt.timezone.utc)

        raw_metrics = _metric_list_to_dict(args.get("metrics") or [])
        try:
            metrics = _validate_metrics(workout_type, raw_metrics)
        except Exception as exc:
            return ToolResult(f"Metric validation error for {workout_type.name}: {exc}")

        entry = WorkoutEntry(
            workout_type_id=workout_type.id,
            performed_at=performed_at,
            performed_on=performed_on,
            time_bucket=time_bucket,
            notes=notes,
            metrics=metrics,
        )
        session.add(entry)
        session.commit()

        return ToolResult(
            f"Logged {workout_type.name} on {performed_on.isoformat()} ({time_bucket})",
            entry_id=entry.id,
            entry_type="workout",
        )

    if name == "create_meal":
        meal_name = str(args.get("meal_name") or "").strip()
        if not meal_name:
            return ToolResult("I need a name for the meal.")

        notes = str(args.get("notes") or "").strip() or None
        ingredient_entries = args.get("ingredients") or []
        if not ingredient_entries:
            return ToolResult("I need at least one ingredient for the meal.")

        meal = Meal(name=meal_name, notes=notes)
        session.add(meal)
        session.flush()

        created_ingredients: list[str] = []
        reused_ingredients: list[str] = []

        for entry in ingredient_entries:
            ing_name = str(entry.get("name") or "").strip()
            if not ing_name:
                continue
            servings = float(entry.get("servings") or 1)

            # Look for existing ingredient (case-insensitive)
            existing = session.scalar(
                select(Ingredient).where(
                    func.lower(Ingredient.name) == ing_name.lower()
                )
            )
            if existing:
                ingredient = existing
                reused_ingredients.append(ing_name)
            else:
                ingredient = Ingredient(
                    name=ing_name,
                    serving_size=str(entry.get("serving_size") or "").strip() or None,
                    calories=entry.get("calories"),
                    protein_g=entry.get("protein_g"),
                    carbs_g=entry.get("carbs_g"),
                    fat_g=entry.get("fat_g"),
                    sugar_g=entry.get("sugar_g"),
                )
                session.add(ingredient)
                session.flush()
                created_ingredients.append(ing_name)

            session.add(
                MealIngredient(
                    meal_id=meal.id,
                    ingredient_id=ingredient.id,
                    servings=servings,
                )
            )

        session.commit()

        parts = [f"Created meal '{meal_name}'"]
        if created_ingredients:
            parts.append(f"New ingredients: {', '.join(created_ingredients)}")
        if reused_ingredients:
            parts.append(f"Existing ingredients: {', '.join(reused_ingredients)}")
        return ToolResult(". ".join(parts), entry_id=meal.id, entry_type="meal")

    if name == "log_nutrition":
        logged_on_raw = args.get("logged_on")
        time_bucket = (
            (args.get("time_bucket") or "").strip().lower()
            if args.get("time_bucket")
            else None
        )
        meal_name = (
            str(args.get("meal_name") or "").strip() if args.get("meal_name") else None
        )
        ad_hoc = args.get("ingredients") or []
        notes = str(args.get("notes") or "").strip() or None

        if logged_on_raw:
            try:
                logged_on = dt.date.fromisoformat(str(logged_on_raw))
            except ValueError:
                logged_on = dt.date.today()
        else:
            logged_on = dt.date.today()

        if not time_bucket or time_bucket not in ALLOWED_TIME_BUCKETS:
            time_bucket = _current_time_bucket(dt.datetime.now())

        log = NutritionLog(
            logged_on=logged_on,
            time_bucket=time_bucket,
            notes=notes,
        )

        logged_items: list[str] = []
        use_ad_hoc = False

        if meal_name:
            meal = session.scalar(
                select(Meal).where(func.lower(Meal.name) == meal_name.lower())
            )
            if meal:
                log.meal_id = meal.id
                session.add(log)
                session.flush()
                # Copy meal ingredients into log items
                for mi in meal.ingredients:
                    session.add(
                        NutritionLogItem(
                            nutrition_log_id=log.id,
                            ingredient_id=mi.ingredient_id,
                            servings=mi.servings,
                        )
                    )
                    logged_items.append(mi.ingredient.name if mi.ingredient else "?")
            elif ad_hoc:
                # Meal not found but ingredients provided — use the ingredients
                logger.warning(
                    "Meal '%s' not found, falling back to ad-hoc ingredients",
                    meal_name,
                )
                use_ad_hoc = True
            else:
                return ToolResult(
                    f"Meal '{meal_name}' not found. Create it first with create_meal."
                )
        else:
            use_ad_hoc = True

        if use_ad_hoc:
            if not ad_hoc:
                return ToolResult("I need either a meal name or ingredients to log.")
            session.add(log)
            session.flush()
            for entry in ad_hoc:
                ing_name = str(entry.get("name") or "").strip()
                if not ing_name:
                    continue
                servings = float(entry.get("servings") or 1)
                ingredient = session.scalar(
                    select(Ingredient).where(
                        func.lower(Ingredient.name) == ing_name.lower()
                    )
                )
                if not ingredient:
                    # Create the ingredient on-the-fly
                    ingredient = Ingredient(
                        name=ing_name,
                        serving_size=str(entry.get("serving_size") or "").strip()
                        or None,
                        calories=entry.get("calories"),
                        protein_g=entry.get("protein_g"),
                        carbs_g=entry.get("carbs_g"),
                        fat_g=entry.get("fat_g"),
                        sugar_g=entry.get("sugar_g"),
                    )
                    session.add(ingredient)
                    session.flush()
                session.add(
                    NutritionLogItem(
                        nutrition_log_id=log.id,
                        ingredient_id=ingredient.id,
                        servings=servings,
                    )
                )
                logged_items.append(ing_name)

        session.commit()
        return ToolResult(
            f"Logged nutrition for {logged_on.isoformat()} ({time_bucket}): {', '.join(logged_items)}",
            entry_id=log.id,
            entry_type="nutrition",
        )

    if name == "list_conversation_entries":
        if not conversation_id:
            return ToolResult("No conversation context available.")
        info_msgs = session.scalars(
            select(AiMessage)
            .where(AiMessage.conversation_id == conversation_id)
            .where(AiMessage.meta["kind"].as_string() == "info")
            .order_by(AiMessage.created_at.asc())
        ).all()
        entries: list[str] = []
        for msg in info_msgs:
            eid = (msg.meta or {}).get("entry_id")
            etype = (msg.meta or {}).get("entry_type")
            if eid and etype:
                entries.append(f"- [{etype}] id={eid}: {msg.content}")
        if not entries:
            return ToolResult("No entries logged in this conversation yet.")
        return ToolResult("Entries logged in this conversation:\n" + "\n".join(entries))

    if name == "edit_entry":
        entry_id = str(args.get("entry_id") or "").strip()
        entry_type = str(args.get("entry_type") or "").strip()
        updates = args.get("updates") or {}
        if not entry_id or not entry_type:
            return ToolResult("entry_id and entry_type are required.")

        if entry_type == "nutrition":
            log = session.get(NutritionLog, entry_id)
            if not log:
                return ToolResult(f"Nutrition log {entry_id} not found.")
            # Update date/time_bucket/notes
            if updates.get("logged_on"):
                try:
                    log.logged_on = dt.date.fromisoformat(str(updates["logged_on"]))
                except ValueError:
                    pass
            if (
                updates.get("time_bucket")
                and updates["time_bucket"] in ALLOWED_TIME_BUCKETS
            ):
                log.time_bucket = updates["time_bucket"]
            if "notes" in updates:
                log.notes = str(updates["notes"] or "").strip() or None
            # Replace ingredients if provided
            new_ingredients = updates.get("ingredients")
            if new_ingredients is not None:
                # Remove old items
                for item in list(log.items):
                    session.delete(item)
                session.flush()
                for entry in new_ingredients:
                    ing_name = str(entry.get("name") or "").strip()
                    if not ing_name:
                        continue
                    servings = float(entry.get("servings") or 1)
                    ingredient = session.scalar(
                        select(Ingredient).where(
                            func.lower(Ingredient.name) == ing_name.lower()
                        )
                    )
                    if not ingredient:
                        ingredient = Ingredient(
                            name=ing_name,
                            serving_size=str(entry.get("serving_size") or "").strip()
                            or None,
                            calories=entry.get("calories"),
                            protein_g=entry.get("protein_g"),
                            carbs_g=entry.get("carbs_g"),
                            fat_g=entry.get("fat_g"),
                            sugar_g=entry.get("sugar_g"),
                        )
                        session.add(ingredient)
                        session.flush()
                    session.add(
                        NutritionLogItem(
                            nutrition_log_id=log.id,
                            ingredient_id=ingredient.id,
                            servings=servings,
                        )
                    )
            session.commit()
            return ToolResult(
                f"Updated nutrition log {entry_id}.",
                entry_id=entry_id,
                entry_type="nutrition",
            )

        if entry_type == "sleep":
            entry = session.get(SleepEntry, entry_id)
            if not entry:
                return ToolResult(f"Sleep entry {entry_id} not found.")
            if updates.get("slept_on"):
                try:
                    entry.slept_on = dt.date.fromisoformat(str(updates["slept_on"]))
                except ValueError:
                    pass
            dh = updates.get("duration_hours")
            dm = updates.get("duration_minutes")
            if dh is not None or dm is not None:
                h = int(dh) if dh is not None else entry.duration_minutes // 60
                m = int(dm) if dm is not None else entry.duration_minutes % 60
                entry.duration_minutes = h * 60 + m
            if "quality" in updates and updates["quality"] is not None:
                try:
                    q = int(updates["quality"])
                    if 1 <= q <= 5:
                        entry.quality = q
                except (ValueError, TypeError):
                    pass
            if "notes" in updates:
                entry.notes = str(updates["notes"] or "").strip() or None
            session.commit()
            return ToolResult(
                f"Updated sleep entry {entry_id}.",
                entry_id=entry_id,
                entry_type="sleep",
            )

        if entry_type == "workout":
            entry = session.get(WorkoutEntry, entry_id)
            if not entry:
                return ToolResult(f"Workout entry {entry_id} not found.")
            if updates.get("performed_on"):
                try:
                    entry.performed_on = dt.date.fromisoformat(
                        str(updates["performed_on"])
                    )
                except ValueError:
                    pass
            if (
                updates.get("time_bucket")
                and updates["time_bucket"] in ALLOWED_TIME_BUCKETS
            ):
                entry.time_bucket = updates["time_bucket"]
            if "notes" in updates:
                entry.notes = str(updates["notes"] or "").strip() or None
            session.commit()
            return ToolResult(
                f"Updated workout entry {entry_id}.",
                entry_id=entry_id,
                entry_type="workout",
            )

        if entry_type == "caffeine":
            entry = session.get(CaffeineEntry, entry_id)
            if not entry:
                return ToolResult(f"Caffeine entry {entry_id} not found.")
            if updates.get("consumed_on"):
                try:
                    entry.consumed_on = dt.date.fromisoformat(
                        str(updates["consumed_on"])
                    )
                except ValueError:
                    pass
            if (
                updates.get("time_bucket")
                and updates["time_bucket"] in ALLOWED_TIME_BUCKETS
            ):
                entry.time_bucket = updates["time_bucket"]
            if "amount_mg" in updates and updates["amount_mg"] is not None:
                try:
                    entry.amount_mg = int(updates["amount_mg"])
                except (ValueError, TypeError):
                    pass
            if "source" in updates:
                entry.source = str(updates["source"] or "").strip() or None
            if "notes" in updates:
                entry.notes = str(updates["notes"] or "").strip() or None
            session.commit()
            return ToolResult(
                f"Updated caffeine entry {entry_id}.",
                entry_id=entry_id,
                entry_type="caffeine",
            )

        return ToolResult(f"Unknown entry type: {entry_type}")

    if name == "delete_entry":
        entry_id = str(args.get("entry_id") or "").strip()
        entry_type = str(args.get("entry_type") or "").strip()
        if not entry_id or not entry_type:
            return ToolResult("entry_id and entry_type are required.")

        model_map = {
            "nutrition": NutritionLog,
            "sleep": SleepEntry,
            "workout": WorkoutEntry,
            "caffeine": CaffeineEntry,
            "meal": Meal,
        }
        model = model_map.get(entry_type)
        if not model:
            return ToolResult(f"Unknown entry type: {entry_type}")
        obj = session.get(model, entry_id)
        if not obj:
            return ToolResult(f"{entry_type} entry {entry_id} not found.")
        session.delete(obj)
        session.commit()
        return ToolResult(f"Deleted {entry_type} entry {entry_id}.")

    if name == "ask_followup":
        return ToolResult(
            str(args.get("question") or "Could you provide more details?")
        )

    return ToolResult(f"Unknown action: {name}")


_NUTRITION_FIELDS = [
    "calories",
    "protein_g",
    "carbs_g",
    "fat_g",
    "fiber_g",
    "sugar_g",
    "caffeine_mg",
]


def get_ingredients_with_missing_info(session) -> list[Ingredient]:
    """Return ingredients that have at least one null nutrition field."""
    all_ings = list(
        session.scalars(select(Ingredient).order_by(Ingredient.name.asc())).all()
    )
    return [
        ing
        for ing in all_ings
        if any(getattr(ing, f) is None for f in _NUTRITION_FIELDS)
    ]


def fill_missing_ingredient_info(
    session,
    ai_cfg: AiConfig,
    ingredients: list[Ingredient],
) -> dict[str, list[str]]:
    """Use Claude + web search to fill missing nutrition fields on ingredients.

    Returns {"updated": [...], "failed": [...]}.
    """
    if not ai_cfg.enabled or not ai_cfg.api_key:
        return {"updated": [], "failed": ["AI not configured"]}

    client = Anthropic(api_key=ai_cfg.api_key)
    updated = []
    failed = []

    for ing in ingredients:
        missing = [f for f in _NUTRITION_FIELDS if getattr(ing, f) is None]
        if not missing:
            continue

        prompt = (
            f"Look up the nutrition facts for: {ing.name}"
            + (f" (serving size: {ing.serving_size})" if ing.serving_size else "")
            + f"\n\nI need the following per serving: {', '.join(missing)}"
            + "\n\nReturn ONLY a JSON object with the numeric values. "
            "Use null if truly unknown. Example: "
            '{"calories": 150, "protein_g": 5.0, "sugar_g": 12.0}'
        )

        try:
            response = client.messages.create(
                model=ai_cfg.model,
                max_tokens=512,
                tools=[
                    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
                ],
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract text from response
            text = ""
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    text += block.text

            # Parse JSON from the response
            # Find the JSON object in the text
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end == 0:
                failed.append(f"{ing.name}: no JSON in response")
                continue

            data = json.loads(text[start:end])

            changed = False
            for field in missing:
                val = data.get(field)
                if val is not None:
                    try:
                        setattr(ing, field, float(val))
                        changed = True
                    except (ValueError, TypeError):
                        pass

            if changed:
                session.commit()
                updated.append(ing.name)
            else:
                failed.append(f"{ing.name}: no data found")

        except Exception as e:
            failed.append(f"{ing.name}: {e}")

    return {"updated": updated, "failed": failed}
