from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from .nutrition_models import Ingredient, NutritionLog, NutritionLogItem
from .sleep_models import SleepEntry
from .caffeine_models import CaffeineEntry
from .tz import today_pacific
from .workouts_models import WorkoutEntry, WorkoutType

# Days with fewer total calories than this are treated as incomplete logging
# and excluded from nutrition averages/charts.
MIN_CALORIES_FOR_COMPLETE_DAY = 1500


def workout_stats(
    session, start: dt.date, end: dt.date, *, today: dt.date | None = None
) -> dict:
    today = today or today_pacific()

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
    today = today or today_pacific()

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
    today = today or today_pacific()

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

    # Exclude today (incomplete) and days below the calorie floor from
    # both averages AND chart data.
    today_str = today.isoformat()
    complete_days = {
        d
        for d in daily_cals
        if d != today_str and daily_cals[d] >= MIN_CALORIES_FOR_COMPLETE_DAY
    }
    num_days = len(complete_days) or 1

    # Strip incomplete days from chart series
    filtered_cals = {d: daily_cals[d] for d in complete_days}
    filtered_protein = {d: daily_protein.get(d, 0) for d in complete_days}
    filtered_carbs = {d: daily_carbs.get(d, 0) for d in complete_days}
    filtered_fat = {d: daily_fat.get(d, 0) for d in complete_days}
    filtered_sugar = {d: daily_sugar.get(d, 0) for d in complete_days}

    return {
        "count": len(logs),
        "avg_calories": sum(filtered_cals.values()) / num_days,
        "avg_protein_g": sum(filtered_protein.values()) / num_days,
        "avg_carbs_g": sum(filtered_carbs.values()) / num_days,
        "avg_fat_g": sum(filtered_fat.values()) / num_days,
        "avg_sugar_g": sum(filtered_sugar.values()) / num_days,
        "daily_calories": filtered_cals,
        "daily_protein": filtered_protein,
        "daily_carbs": filtered_carbs,
        "daily_fat": filtered_fat,
        "daily_sugar": filtered_sugar,
    }


# ---------------------------------------------------------------------------
# Simple linear regression helpers (no numpy dependency)
# ---------------------------------------------------------------------------


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _ols_multi(X: list[list[float]], y: list[float]) -> list[float]:
    """Ordinary least squares for multiple features.

    Returns coefficients [intercept, b1, b2, ...].  Falls back to the mean
    of *y* (all-zero coefficients) when the system is under-determined or
    singular.
    """
    n = len(y)
    if n == 0:
        return []
    k = len(X[0]) if X else 0
    if n <= k + 1:
        # Not enough data points for regression — return mean-only model.
        return [_mean(y)] + [0.0] * k

    # Build X matrix with intercept column
    A = [[1.0] + row for row in X]
    cols = k + 1

    # A^T A
    ATA = [[0.0] * cols for _ in range(cols)]
    ATy = [0.0] * cols
    for i in range(n):
        for j in range(cols):
            ATy[j] += A[i][j] * y[i]
            for m in range(cols):
                ATA[j][m] += A[i][j] * A[i][m]

    # Gaussian elimination with partial pivoting
    aug = [ATA[r][:] + [ATy[r]] for r in range(cols)]
    for col in range(cols):
        # pivot
        max_row = max(range(col, cols), key=lambda r: abs(aug[r][col]))
        aug[col], aug[max_row] = aug[max_row], aug[col]
        if abs(aug[col][col]) < 1e-12:
            return [_mean(y)] + [0.0] * k
        for row in range(cols):
            if row == col:
                continue
            factor = aug[row][col] / aug[col][col]
            for j in range(cols + 1):
                aug[row][j] -= factor * aug[col][j]

    return [
        aug[i][cols] / aug[i][i] if abs(aug[i][i]) > 1e-12 else 0.0 for i in range(cols)
    ]


def _predict(coeffs: list[float], features: list[float]) -> float:
    if not coeffs:
        return 0.0
    return coeffs[0] + sum(c * f for c, f in zip(coeffs[1:], features))


# ---------------------------------------------------------------------------
# Pickleball insights
# ---------------------------------------------------------------------------

PICKLEBALL_PERF_KEY = "How well did you play 1-5"


def _get_daily_caffeine(session, dates: set[dt.date]) -> dict[dt.date, float]:
    """Total caffeine mg per day (standalone entries + nutrition ingredients)."""
    if not dates:
        return {}
    min_d, max_d = min(dates), max(dates)

    result: dict[dt.date, float] = {}

    # Standalone caffeine entries
    for e in session.scalars(
        select(CaffeineEntry).where(
            CaffeineEntry.consumed_on >= min_d,
            CaffeineEntry.consumed_on <= max_d,
        )
    ).all():
        if e.consumed_on in dates:
            result[e.consumed_on] = result.get(e.consumed_on, 0) + e.amount_mg

    # Caffeine from nutrition log ingredients
    for log in session.scalars(
        select(NutritionLog).where(
            NutritionLog.logged_on >= min_d,
            NutritionLog.logged_on <= max_d,
        )
    ).all():
        if log.logged_on in dates:
            result[log.logged_on] = result.get(log.logged_on, 0) + log.total_caffeine_mg

    return result


def _get_daily_sleep(
    session, dates: set[dt.date]
) -> dict[dt.date, tuple[float, float | None]]:
    """Returns {date: (hours, quality_or_None)}."""
    if not dates:
        return {}
    min_d, max_d = min(dates), max(dates)
    result: dict[dt.date, tuple[float, float | None]] = {}
    for e in session.scalars(
        select(SleepEntry).where(
            SleepEntry.slept_on >= min_d,
            SleepEntry.slept_on <= max_d,
        )
    ).all():
        if e.slept_on in dates:
            result[e.slept_on] = (e.duration_minutes / 60.0, e.quality)
    return result


def _get_daily_workouts(session, dates: set[dt.date]) -> dict[dt.date, int]:
    """Number of workout sessions per day."""
    if not dates:
        return {}
    min_d, max_d = min(dates), max(dates)
    result: dict[dt.date, int] = {}
    for e in session.scalars(
        select(WorkoutEntry).where(
            WorkoutEntry.performed_on >= min_d,
            WorkoutEntry.performed_on <= max_d,
        )
    ).all():
        if e.performed_on in dates:
            result[e.performed_on] = result.get(e.performed_on, 0) + 1
    return result


def pickleball_insights(session) -> dict:
    """Gather pickleball performance data correlated with sleep, caffeine,
    and workouts.  Returns chart-ready data plus simple OLS predictions."""

    # Find the Pickleball workout type
    pb_type = session.scalars(
        select(WorkoutType).where(WorkoutType.name == "Pickleball")
    ).first()
    if not pb_type:
        return {"has_data": False}

    entries = list(
        session.scalars(
            select(WorkoutEntry)
            .where(WorkoutEntry.workout_type_id == pb_type.id)
            .order_by(WorkoutEntry.performed_on.asc())
        ).all()
    )
    if not entries:
        return {"has_data": False}

    # Extract performance per day (average if multiple sessions same day)
    day_perf: dict[dt.date, list[float]] = {}
    day_drill_play: dict[dt.date, list[str]] = {}
    for e in entries:
        d = e.performed_on
        if not d:
            continue
        metrics = e.metrics or {}
        rating = metrics.get(PICKLEBALL_PERF_KEY)
        if rating is not None:
            day_perf.setdefault(d, []).append(float(rating))
        dp = metrics.get("drill/play")
        if dp:
            day_drill_play.setdefault(d, []).append(dp)

    pb_dates = sorted(day_perf.keys())
    if not pb_dates:
        return {"has_data": False}

    # Collect prior-day dates we need context for
    prior_dates = {d - dt.timedelta(days=1) for d in pb_dates}
    all_dates = set(pb_dates) | prior_dates

    daily_caffeine = _get_daily_caffeine(session, all_dates)
    daily_sleep = _get_daily_sleep(session, all_dates)
    daily_workouts = _get_daily_workouts(session, all_dates)

    # Build per-pickleball-day data points
    points: list[dict] = []
    for d in pb_dates:
        prev = d - dt.timedelta(days=1)
        perf = _mean(day_perf[d])
        sleep_h, sleep_q = daily_sleep.get(prev, (None, None))
        caffeine = daily_caffeine.get(prev, 0)
        workouts = daily_workouts.get(prev, 0)
        points.append(
            {
                "date": d.isoformat(),
                "performance": round(perf, 2),
                "drill_play": day_drill_play.get(d, ["play"])[0],
                "prev_sleep_hours": round(sleep_h, 1) if sleep_h is not None else None,
                "prev_sleep_quality": sleep_q,
                "prev_caffeine_mg": round(caffeine, 1),
                "prev_workouts": workouts,
                # Same-day context too
                "same_day_caffeine_mg": round(daily_caffeine.get(d, 0), 1),
                "same_day_sleep_hours": round(daily_sleep.get(d, (None, None))[0], 1)
                if daily_sleep.get(d, (None, None))[0] is not None
                else None,
                "same_day_sleep_quality": daily_sleep.get(d, (None, None))[1],
            }
        )

    # -- Pickleball performance prediction model --
    # Features: prev sleep hours, prev sleep quality, prev caffeine, prev workouts
    perf_X: list[list[float]] = []
    perf_y: list[float] = []
    for p in points:
        if p["prev_sleep_hours"] is not None and p["prev_sleep_quality"] is not None:
            perf_X.append(
                [
                    p["prev_sleep_hours"],
                    float(p["prev_sleep_quality"]),
                    p["prev_caffeine_mg"],
                    float(p["prev_workouts"]),
                ]
            )
            perf_y.append(p["performance"])

    perf_coeffs = _ols_multi(perf_X, perf_y)

    # Add predicted performance to each point
    for p in points:
        if p["prev_sleep_hours"] is not None and p["prev_sleep_quality"] is not None:
            pred = _predict(
                perf_coeffs,
                [
                    p["prev_sleep_hours"],
                    float(p["prev_sleep_quality"]),
                    p["prev_caffeine_mg"],
                    float(p["prev_workouts"]),
                ],
            )
            p["predicted_performance"] = round(max(1, min(5, pred)), 2)
        else:
            p["predicted_performance"] = None

    # -- Sleep prediction model --
    # For every day with sleep data, predict sleep hours from prior day's
    # caffeine, workouts, and (prior) sleep.
    all_sleep_dates = sorted(daily_sleep.keys())
    sleep_points: list[dict] = []
    sleep_X: list[list[float]] = []
    sleep_y: list[float] = []

    for d in all_sleep_dates:
        prev = d - dt.timedelta(days=1)
        hours, quality = daily_sleep[d]
        prev_caff = daily_caffeine.get(prev, 0)
        prev_wk = daily_workouts.get(prev, 0)
        prev_sleep_h, prev_sleep_q = daily_sleep.get(prev, (None, None))
        sp = {
            "date": d.isoformat(),
            "sleep_hours": round(hours, 1),
            "sleep_quality": quality,
            "prev_caffeine_mg": round(prev_caff, 1),
            "prev_workouts": prev_wk,
            "prev_sleep_hours": round(prev_sleep_h, 1)
            if prev_sleep_h is not None
            else None,
        }
        sleep_points.append(sp)
        # Only include in regression if we have prior day data
        if prev_sleep_h is not None:
            sleep_X.append([prev_sleep_h, prev_caff, float(prev_wk)])
            sleep_y.append(hours)

    sleep_coeffs = _ols_multi(sleep_X, sleep_y)
    for sp in sleep_points:
        if sp["prev_sleep_hours"] is not None:
            pred = _predict(
                sleep_coeffs,
                [
                    sp["prev_sleep_hours"],
                    sp["prev_caffeine_mg"],
                    float(sp["prev_workouts"]),
                ],
            )
            sp["predicted_sleep_hours"] = round(max(0, pred), 1)
        else:
            sp["predicted_sleep_hours"] = None

    # Feature labels for display
    perf_feature_names = [
        "Prior sleep (hrs)",
        "Prior sleep quality",
        "Prior caffeine (mg)",
        "Prior workouts",
    ]
    sleep_feature_names = [
        "Prior sleep (hrs)",
        "Prior caffeine (mg)",
        "Prior workouts",
    ]

    return {
        "has_data": True,
        "pickleball": points,
        "sleep": sleep_points,
        "perf_coefficients": {
            name: round(c, 4) for name, c in zip(perf_feature_names, perf_coeffs[1:])
        }
        if len(perf_coeffs) > 1
        else {},
        "sleep_coefficients": {
            name: round(c, 4) for name, c in zip(sleep_feature_names, sleep_coeffs[1:])
        }
        if len(sleep_coeffs) > 1
        else {},
        "data_points": len(perf_y),
        "sleep_data_points": len(sleep_y),
    }
