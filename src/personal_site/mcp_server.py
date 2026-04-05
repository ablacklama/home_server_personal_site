"""MCP server exposing personal-site health data to Claude.

Run with:  uv run python -m personal_site.mcp_server
Or point Claude's MCP config at this module.
"""

from __future__ import annotations

import datetime as dt
import os

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.orm import Session

from .caffeine_models import CaffeineEntry
from .db import Base, create_engine_and_sessionmaker, ensure_sqlite_workouts_schema
from .nutrition_models import Ingredient, Meal, NutritionLog
from .sleep_models import SleepEntry
from .tz import today_pacific
from .workouts_models import WorkoutEntry, WorkoutType

mcp = FastMCP("personal-site")

# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

_SessionLocal = None


def _get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        db_url = os.environ.get(
            "DATABASE_URL", "sqlite:///./data/personal_site.sqlite3"
        )
        engine, _SessionLocal = create_engine_and_sessionmaker(db_url)
        Base.metadata.create_all(bind=engine)
        ensure_sqlite_workouts_schema(engine)
    return _SessionLocal()


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_daily_summary(date: str | None = None) -> dict:
    """Get a full summary of a single day: sleep, nutrition, caffeine, workouts.

    Args:
        date: ISO date (YYYY-MM-DD). Defaults to today (Pacific time).
    """
    day = _parse_date(date) or today_pacific()

    with _get_session() as session:
        # Sleep
        sleep_entry = session.scalars(
            select(SleepEntry)
            .where(SleepEntry.slept_on == day)
            .order_by(SleepEntry.created_at.desc())
            .limit(1)
        ).first()
        sleep = None
        if sleep_entry:
            sleep = {
                "hours": sleep_entry.duration_minutes // 60,
                "minutes": sleep_entry.duration_minutes % 60,
                "quality": sleep_entry.quality,
                "notes": sleep_entry.notes,
            }

        # Nutrition
        logs = list(
            session.scalars(
                select(NutritionLog).where(NutritionLog.logged_on == day)
            ).all()
        )
        meals = []
        total_cal = total_p = total_c = total_f = total_s = total_caff = 0.0
        for log in logs:
            name = log.meal.name if log.meal else "Ad-hoc"
            if not log.meal and log.items:
                names = [it.ingredient.name for it in log.items[:4]]
                name = ", ".join(names)
                if len(log.items) > 4:
                    name += f" +{len(log.items) - 4}"
            cal = log.total_calories
            total_cal += cal
            total_p += log.total_protein_g
            total_c += log.total_carbs_g
            total_f += log.total_fat_g
            total_s += log.total_sugar_g
            total_caff += log.total_caffeine_mg
            meals.append(
                {
                    "name": name,
                    "time_bucket": log.time_bucket,
                    "calories": round(cal, 1),
                    "protein_g": round(log.total_protein_g, 1),
                    "carbs_g": round(log.total_carbs_g, 1),
                    "fat_g": round(log.total_fat_g, 1),
                }
            )

        # Caffeine (standalone entries)
        caffeine_entries = list(
            session.scalars(
                select(CaffeineEntry).where(CaffeineEntry.consumed_on == day)
            ).all()
        )
        standalone_caff = sum(e.amount_mg for e in caffeine_entries)
        caffeine_total = standalone_caff + total_caff

        caffeine_detail = [
            {"source": e.source, "amount_mg": e.amount_mg, "time_bucket": e.time_bucket}
            for e in caffeine_entries
        ]

        # Workouts
        workout_entries = list(
            session.scalars(
                select(WorkoutEntry).where(WorkoutEntry.performed_on == day)
            ).all()
        )
        type_ids = {e.workout_type_id for e in workout_entries}
        type_map: dict[str, WorkoutType] = {}
        if type_ids:
            for wt in session.scalars(
                select(WorkoutType).where(WorkoutType.id.in_(type_ids))
            ).all():
                type_map[wt.id] = wt

        workouts = []
        for e in workout_entries:
            wt = type_map.get(e.workout_type_id)
            workouts.append(
                {
                    "type": wt.name if wt else "Unknown",
                    "time_bucket": e.time_bucket,
                    "metrics": e.metrics,
                    "notes": e.notes,
                }
            )

    return {
        "date": day.isoformat(),
        "sleep": sleep,
        "nutrition": {
            "total_calories": round(total_cal, 1),
            "total_protein_g": round(total_p, 1),
            "total_carbs_g": round(total_c, 1),
            "total_fat_g": round(total_f, 1),
            "total_sugar_g": round(total_s, 1),
            "meals": meals,
        },
        "caffeine_mg": round(caffeine_total, 1),
        "caffeine_detail": caffeine_detail,
        "workouts": workouts,
    }


@mcp.tool()
def get_workouts(
    start_date: str | None = None,
    end_date: str | None = None,
    workout_type: str | None = None,
) -> list[dict]:
    """Query workout entries within a date range.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date (YYYY-MM-DD). Defaults to today.
        workout_type: Filter by workout type name (e.g. "Pickleball").
    """
    today = today_pacific()
    start = _parse_date(start_date) or (today - dt.timedelta(days=30))
    end = _parse_date(end_date) or today

    with _get_session() as session:
        q = select(WorkoutEntry).where(
            WorkoutEntry.performed_on >= start,
            WorkoutEntry.performed_on <= end,
        )

        # Resolve type filter
        type_id = None
        if workout_type:
            wt = session.scalars(
                select(WorkoutType).where(WorkoutType.name.ilike(workout_type))
            ).first()
            if wt:
                type_id = wt.id
                q = q.where(WorkoutEntry.workout_type_id == type_id)
            else:
                return []

        entries = list(
            session.scalars(q.order_by(WorkoutEntry.performed_on.asc())).all()
        )

        # Build type name map
        all_type_ids = {e.workout_type_id for e in entries}
        type_map = {}
        if all_type_ids:
            for wt in session.scalars(
                select(WorkoutType).where(WorkoutType.id.in_(all_type_ids))
            ).all():
                type_map[wt.id] = wt.name

        return [
            {
                "date": e.performed_on.isoformat() if e.performed_on else None,
                "type": type_map.get(e.workout_type_id, "Unknown"),
                "time_bucket": e.time_bucket,
                "metrics": e.metrics,
                "notes": e.notes,
            }
            for e in entries
        ]


@mcp.tool()
def get_sleep(
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Query sleep entries within a date range.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date (YYYY-MM-DD). Defaults to today.
    """
    today = today_pacific()
    start = _parse_date(start_date) or (today - dt.timedelta(days=30))
    end = _parse_date(end_date) or today

    with _get_session() as session:
        entries = list(
            session.scalars(
                select(SleepEntry)
                .where(SleepEntry.slept_on >= start, SleepEntry.slept_on <= end)
                .order_by(SleepEntry.slept_on.asc())
            ).all()
        )
        return [
            {
                "date": e.slept_on.isoformat(),
                "hours": e.duration_minutes // 60,
                "minutes": e.duration_minutes % 60,
                "quality": e.quality,
                "notes": e.notes,
            }
            for e in entries
        ]


@mcp.tool()
def get_caffeine(
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Query caffeine entries within a date range. Includes both standalone
    caffeine logs and caffeine from nutrition log ingredients.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date (YYYY-MM-DD). Defaults to today.
    """
    today = today_pacific()
    start = _parse_date(start_date) or (today - dt.timedelta(days=30))
    end = _parse_date(end_date) or today

    with _get_session() as session:
        # Standalone entries
        entries = list(
            session.scalars(
                select(CaffeineEntry)
                .where(
                    CaffeineEntry.consumed_on >= start,
                    CaffeineEntry.consumed_on <= end,
                )
                .order_by(CaffeineEntry.consumed_on.asc())
            ).all()
        )
        results: list[dict] = [
            {
                "date": e.consumed_on.isoformat(),
                "amount_mg": e.amount_mg,
                "source": e.source,
                "time_bucket": e.time_bucket,
                "origin": "standalone",
            }
            for e in entries
        ]

        # From nutrition logs
        logs = list(
            session.scalars(
                select(NutritionLog).where(
                    NutritionLog.logged_on >= start,
                    NutritionLog.logged_on <= end,
                )
            ).all()
        )
        for log in logs:
            caff = log.total_caffeine_mg
            if caff > 0:
                name = log.meal.name if log.meal else "meal"
                results.append(
                    {
                        "date": log.logged_on.isoformat(),
                        "amount_mg": round(caff, 1),
                        "source": f"nutrition: {name}",
                        "time_bucket": log.time_bucket,
                        "origin": "nutrition",
                    }
                )

        results.sort(key=lambda r: r["date"])
        return results


@mcp.tool()
def get_nutrition(
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Query nutrition logs within a date range. Returns per-meal detail
    with macros.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date (YYYY-MM-DD). Defaults to today.
    """
    today = today_pacific()
    start = _parse_date(start_date) or (today - dt.timedelta(days=30))
    end = _parse_date(end_date) or today

    with _get_session() as session:
        logs = list(
            session.scalars(
                select(NutritionLog)
                .where(
                    NutritionLog.logged_on >= start,
                    NutritionLog.logged_on <= end,
                )
                .order_by(NutritionLog.logged_on.asc())
            ).all()
        )

        results = []
        for log in logs:
            name = log.meal.name if log.meal else "Ad-hoc"
            if not log.meal and log.items:
                names = [it.ingredient.name for it in log.items[:4]]
                name = ", ".join(names)
                if len(log.items) > 4:
                    name += f" +{len(log.items) - 4}"

            items = [
                {
                    "ingredient": it.ingredient.name,
                    "servings": it.servings,
                    "calories": round((it.ingredient.calories or 0) * it.servings, 1),
                    "protein_g": round((it.ingredient.protein_g or 0) * it.servings, 1),
                    "carbs_g": round((it.ingredient.carbs_g or 0) * it.servings, 1),
                    "fat_g": round((it.ingredient.fat_g or 0) * it.servings, 1),
                }
                for it in log.items
            ]

            results.append(
                {
                    "date": log.logged_on.isoformat(),
                    "meal_name": name,
                    "time_bucket": log.time_bucket,
                    "total_calories": round(log.total_calories, 1),
                    "total_protein_g": round(log.total_protein_g, 1),
                    "total_carbs_g": round(log.total_carbs_g, 1),
                    "total_fat_g": round(log.total_fat_g, 1),
                    "total_sugar_g": round(log.total_sugar_g, 1),
                    "items": items,
                    "notes": log.notes,
                }
            )
        return results


@mcp.tool()
def list_workout_types() -> list[dict]:
    """List all configured workout types and their metric schemas."""
    with _get_session() as session:
        types = list(
            session.scalars(select(WorkoutType).order_by(WorkoutType.name)).all()
        )
        return [
            {
                "name": wt.name,
                "metric_schema": wt.metric_schema,
                "calories_per_hour": wt.calories_per_hour,
            }
            for wt in types
        ]


@mcp.tool()
def list_saved_meals() -> list[dict]:
    """List all saved meal templates with their ingredients."""
    with _get_session() as session:
        meals = list(session.scalars(select(Meal).order_by(Meal.name)).all())
        results = []
        for m in meals:
            ingredients = []
            for mi in m.ingredients:
                ing = mi.ingredient
                ingredients.append(
                    {
                        "name": ing.name,
                        "servings": mi.servings,
                        "serving_size": ing.serving_size,
                        "calories": ing.calories,
                        "protein_g": ing.protein_g,
                        "carbs_g": ing.carbs_g,
                        "fat_g": ing.fat_g,
                        "caffeine_mg": ing.caffeine_mg,
                    }
                )
            results.append(
                {
                    "name": m.name,
                    "notes": m.notes,
                    "ingredients": ingredients,
                }
            )
        return results


@mcp.tool()
def search_ingredients(query: str) -> list[dict]:
    """Search ingredients by name (case-insensitive substring match).

    Args:
        query: Search string to match against ingredient names.
    """
    with _get_session() as session:
        results = list(
            session.scalars(
                select(Ingredient)
                .where(Ingredient.name.ilike(f"%{query}%"))
                .order_by(Ingredient.name)
                .limit(50)
            ).all()
        )
        return [
            {
                "name": i.name,
                "serving_size": i.serving_size,
                "calories": i.calories,
                "protein_g": i.protein_g,
                "carbs_g": i.carbs_g,
                "fat_g": i.fat_g,
                "sugar_g": i.sugar_g,
                "caffeine_mg": i.caffeine_mg,
            }
            for i in results
        ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run()


if __name__ == "__main__":
    main()
