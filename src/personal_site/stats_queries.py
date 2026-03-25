from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from .nutrition_models import Ingredient, NutritionLog, NutritionLogItem
from .sleep_models import SleepEntry
from .workouts_models import WorkoutEntry, WorkoutType


def workout_stats(
    session, start: dt.date, end: dt.date, *, today: dt.date | None = None
) -> dict:
    today = today or dt.date.today()

    entries = session.scalars(
        select(WorkoutEntry).where(
            WorkoutEntry.performed_on >= start,
            WorkoutEntry.performed_on <= end,
        )
    ).all()

    total = len(entries)

    # Per-type counts
    type_ids = {e.workout_type_id for e in entries}
    types = (
        {
            t.id: t.name
            for t in session.scalars(
                select(WorkoutType).where(WorkoutType.id.in_(type_ids))
            ).all()
        }
        if type_ids
        else {}
    )

    per_type: dict[str, int] = {}
    for e in entries:
        name = types.get(e.workout_type_id, "Unknown")
        per_type[name] = per_type.get(name, 0) + 1

    # Per-day counts for charting (include today for the chart)
    daily: dict[str, int] = {}
    for e in entries:
        day = e.performed_on.isoformat() if e.performed_on else "unknown"
        daily[day] = daily.get(day, 0) + 1

    # Streak: consecutive days with at least one workout ending at today
    workout_dates = sorted({e.performed_on for e in entries if e.performed_on})
    streak = 0
    if workout_dates:
        check = end
        date_set = set(workout_dates)
        while check >= start and check in date_set:
            streak += 1
            check -= dt.timedelta(days=1)

    return {
        "total": total,
        "per_type": per_type,
        "daily": daily,
        "streak": streak,
    }


def sleep_stats(
    session, start: dt.date, end: dt.date, *, today: dt.date | None = None
) -> dict:
    today = today or dt.date.today()

    entries = list(
        session.scalars(
            select(SleepEntry).where(
                SleepEntry.slept_on >= start,
                SleepEntry.slept_on <= end,
            )
        ).all()
    )

    if not entries:
        return {
            "count": 0,
            "avg_duration_minutes": 0,
            "avg_quality": None,
            "daily": {},
        }

    total_minutes = sum(e.duration_minutes for e in entries)
    qualities = [e.quality for e in entries if e.quality is not None]

    # Daily values for charting (include today)
    daily: dict[str, float] = {}
    for e in entries:
        day = e.slept_on.isoformat()
        daily[day] = e.duration_minutes / 60.0

    # For averages, exclude today (incomplete day)
    completed = [e for e in entries if e.slept_on < today]
    if completed:
        avg_min = sum(e.duration_minutes for e in completed) / len(completed)
        comp_q = [e.quality for e in completed if e.quality is not None]
        avg_q = sum(comp_q) / len(comp_q) if comp_q else None
    else:
        avg_min = total_minutes / len(entries)
        avg_q = sum(qualities) / len(qualities) if qualities else None

    return {
        "count": len(entries),
        "avg_duration_minutes": avg_min,
        "avg_quality": avg_q,
        "daily": daily,
    }


def nutrition_stats(
    session, start: dt.date, end: dt.date, *, today: dt.date | None = None
) -> dict:
    today = today or dt.date.today()

    logs = list(
        session.scalars(
            select(NutritionLog).where(
                NutritionLog.logged_on >= start,
                NutritionLog.logged_on <= end,
            )
        ).all()
    )

    if not logs:
        return {
            "count": 0,
            "avg_calories": 0,
            "avg_protein_g": 0,
            "avg_carbs_g": 0,
            "avg_fat_g": 0,
            "avg_sugar_g": 0,
            "daily_calories": {},
        }

    # Group by day for daily totals
    daily_cals: dict[str, float] = {}
    daily_protein: dict[str, float] = {}
    daily_carbs: dict[str, float] = {}
    daily_fat: dict[str, float] = {}
    daily_sugar: dict[str, float] = {}

    for log in logs:
        day = log.logged_on.isoformat()
        # Load items for this log
        items = list(
            session.scalars(
                select(NutritionLogItem).where(
                    NutritionLogItem.nutrition_log_id == log.id
                )
            ).all()
        )
        for item in items:
            ingredient = session.get(Ingredient, item.ingredient_id)
            if not ingredient:
                continue
            daily_cals[day] = (
                daily_cals.get(day, 0) + (ingredient.calories or 0) * item.servings
            )
            daily_protein[day] = (
                daily_protein.get(day, 0) + (ingredient.protein_g or 0) * item.servings
            )
            daily_carbs[day] = (
                daily_carbs.get(day, 0) + (ingredient.carbs_g or 0) * item.servings
            )
            daily_fat[day] = (
                daily_fat.get(day, 0) + (ingredient.fat_g or 0) * item.servings
            )
            daily_sugar[day] = (
                daily_sugar.get(day, 0) + (ingredient.sugar_g or 0) * item.servings
            )

    # For averages, exclude today (incomplete day) so partial logging
    # doesn't drag down the average.
    today_str = today.isoformat()
    completed_days = {d for d in daily_cals if d != today_str}
    num_days = len(completed_days) or 1

    return {
        "count": len(logs),
        "avg_calories": sum(daily_cals.get(d, 0) for d in completed_days) / num_days,
        "avg_protein_g": sum(daily_protein.get(d, 0) for d in completed_days)
        / num_days,
        "avg_carbs_g": sum(daily_carbs.get(d, 0) for d in completed_days) / num_days,
        "avg_fat_g": sum(daily_fat.get(d, 0) for d in completed_days) / num_days,
        "avg_sugar_g": sum(daily_sugar.get(d, 0) for d in completed_days) / num_days,
        "daily_calories": daily_cals,
    }
