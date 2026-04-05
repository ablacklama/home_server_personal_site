from __future__ import annotations

import datetime as dt
import json

from flask import (
    Blueprint,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .activity_log import log_activity
from .ai import (
    AiConfig,
    fill_missing_ingredient_info,
    get_ingredients_with_missing_info,
)
from .nutrition_models import (
    Ingredient,
    Meal,
    MealIngredient,
    NutritionLog,
    NutritionLogItem,
)
from .tz import now_pacific, today_pacific
from .workouts import ALLOWED_TIME_BUCKETS, _current_time_bucket

bp = Blueprint("nutrition", __name__, url_prefix="/nutrition")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _make_htmx_response(
    template_name: str, context: dict, status: int = 200, trigger: dict | None = None
):
    response = make_response(render_template(template_name, **context), status)
    if trigger:
        response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_ingredients(session) -> list[Ingredient]:
    return list(
        session.scalars(select(Ingredient).order_by(Ingredient.name.asc())).all()
    )


def _all_meals(session) -> list[Meal]:
    return list(
        session.scalars(
            select(Meal)
            .options(joinedload(Meal.ingredients).joinedload(MealIngredient.ingredient))
            .order_by(Meal.name.asc())
        )
        .unique()
        .all()
    )


def _recent_logs(session, limit: int = 25) -> list[NutritionLog]:
    return list(
        session.scalars(
            select(NutritionLog)
            .options(
                joinedload(NutritionLog.items).joinedload(NutritionLogItem.ingredient),
                joinedload(NutritionLog.meal),
            )
            .order_by(NutritionLog.created_at.desc())
            .limit(limit)
        )
        .unique()
        .all()
    )


def _load_index_context(session, error: str | None = None) -> dict:
    now = now_pacific()
    return {
        "ingredients": _all_ingredients(session),
        "meals": _all_meals(session),
        "recent_logs": _recent_logs(session),
        "default_logged_on": today_pacific().isoformat(),
        "default_time_bucket": _current_time_bucket(now),
        "error": error,
    }


def _render_index(message: str | None = None):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "nutrition.html",
            title="Nutrition",
            ingredients=[],
            meals=[],
            recent_logs=[],
            default_logged_on=today_pacific().isoformat(),
            default_time_bucket=_current_time_bucket(now_pacific()),
            error=message
            or current_app.config.get("DB_ERROR")
            or "DATABASE_URL is not set",
        )

    with SessionLocal() as session:
        ctx = _load_index_context(session, error=message)
    return render_template("nutrition.html", title="Nutrition", **ctx)


def _render_log_response(
    session, message: str | None = None, status: int = 200, trigger: dict | None = None
):
    ctx = _load_index_context(session, error=message)
    ctx["oob"] = True
    return _make_htmx_response(
        "partials/nutrition_entry_response.html", ctx, status=status, trigger=trigger
    )


def _load_ingredients_context(session, error: str | None = None) -> dict:
    ingredients = _all_ingredients(session)
    has_missing = bool(get_ingredients_with_missing_info(session))
    return {
        "ingredients": ingredients,
        "has_missing_info": has_missing,
        "error": error,
    }


def _render_ingredients(message: str | None = None):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "nutrition_ingredients.html",
            title="Ingredients",
            ingredients=[],
            has_missing_info=False,
            error=message or "DATABASE_URL is not set",
        )
    with SessionLocal() as session:
        ctx = _load_ingredients_context(session, error=message)
    return render_template("nutrition_ingredients.html", title="Ingredients", **ctx)


def _render_ingredient_response(
    session, message: str | None = None, status: int = 200, trigger: dict | None = None
):
    ctx = _load_ingredients_context(session, error=message)
    ctx["oob"] = True
    return _make_htmx_response(
        "partials/nutrition_ingredient_response.html",
        ctx,
        status=status,
        trigger=trigger,
    )


def _load_meals_context(session, error: str | None = None) -> dict:
    return {
        "meals": _all_meals(session),
        "ingredients": _all_ingredients(session),
        "error": error,
    }


def _render_meals(message: str | None = None):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "nutrition_meals.html",
            title="Meals",
            meals=[],
            ingredients=[],
            error=message or "DATABASE_URL is not set",
        )
    with SessionLocal() as session:
        ctx = _load_meals_context(session, error=message)
    return render_template("nutrition_meals.html", title="Meals", **ctx)


def _render_meal_response(
    session, message: str | None = None, status: int = 200, trigger: dict | None = None
):
    ctx = _load_meals_context(session, error=message)
    ctx["oob"] = True
    return _make_htmx_response(
        "partials/nutrition_meal_response.html",
        ctx,
        status=status,
        trigger=trigger,
    )


# ---------------------------------------------------------------------------
# Nutrition log routes
# ---------------------------------------------------------------------------


@bp.get("/")
def index():
    return _render_index()


@bp.post("/logs")
def create_log():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _make_htmx_response(
                "partials/nutrition_entry_response.html",
                {
                    "recent_logs": [],
                    "ingredients": [],
                    "meals": [],
                    "error": message,
                    "oob": True,
                },
                status=503,
            )
        return _render_index(message), 503

    with SessionLocal() as session:
        logged_on_raw = (request.form.get("logged_on") or "").strip()
        time_bucket = (request.form.get("time_bucket") or "").strip().lower()
        meal_id = (request.form.get("meal_id") or "").strip() or None
        notes = (request.form.get("notes") or "").strip() or None

        # Parse date
        if logged_on_raw:
            try:
                logged_on = dt.date.fromisoformat(logged_on_raw)
            except ValueError:
                msg = "Invalid date"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400
        else:
            logged_on = today_pacific()

        # Time bucket
        if not time_bucket:
            time_bucket = _current_time_bucket(now_pacific())
        if time_bucket not in ALLOWED_TIME_BUCKETS:
            msg = "time bucket must be morning, afternoon, or night"
            if _is_htmx():
                return _render_log_response(session, msg, status=400)
            return _render_index(msg), 400

        # Collect ingredient rows from form
        ingredient_ids = request.form.getlist("ingredient_id")
        servings_raw = request.form.getlist("servings")

        if not ingredient_ids or all(not i.strip() for i in ingredient_ids):
            # If a meal is selected, populate from the meal
            if meal_id:
                meal = session.get(Meal, meal_id)
                if meal is None:
                    msg = "Unknown meal"
                    if _is_htmx():
                        return _render_log_response(session, msg, status=400)
                    return _render_index(msg), 400
                # Load meal ingredients
                meal = (
                    session.scalars(
                        select(Meal)
                        .options(joinedload(Meal.ingredients))
                        .where(Meal.id == meal_id)
                    )
                    .unique()
                    .one()
                )
                ingredient_ids = [mi.ingredient_id for mi in meal.ingredients]
                servings_raw = [str(mi.servings) for mi in meal.ingredients]
            else:
                msg = "Add at least one ingredient"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400

        # Validate ingredients and build items
        items: list[NutritionLogItem] = []
        for idx, ing_id in enumerate(ingredient_ids):
            ing_id = ing_id.strip()
            if not ing_id:
                continue
            ingredient = session.get(Ingredient, ing_id)
            if ingredient is None:
                msg = f"Unknown ingredient (row {idx + 1})"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400

            srv_raw = servings_raw[idx].strip() if idx < len(servings_raw) else "1"
            try:
                srv = float(srv_raw) if srv_raw else 1.0
            except ValueError:
                msg = f"Invalid servings for {ingredient.name}"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400
            if srv <= 0:
                msg = f"Servings must be > 0 for {ingredient.name}"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400

            items.append(NutritionLogItem(ingredient_id=ing_id, servings=srv))

        if not items:
            msg = "Add at least one ingredient"
            if _is_htmx():
                return _render_log_response(session, msg, status=400)
            return _render_index(msg), 400

        log = NutritionLog(
            logged_on=logged_on,
            time_bucket=time_bucket,
            meal_id=meal_id,
            notes=notes,
            items=items,
        )
        session.add(log)
        session.commit()

        log_activity(
            "nutrition",
            "create_log",
            f"{logged_on} {time_bucket} items={len(items)}",
        )

        if _is_htmx():
            return _render_log_response(session, trigger={"nutritionLogSaved": True})
        return redirect(url_for("nutrition.index"))


@bp.get("/logs/<log_id>/edit")
def edit_log_form(log_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_index("Database is not configured"), 503

    with SessionLocal() as session:
        log = (
            session.scalars(
                select(NutritionLog)
                .options(
                    joinedload(NutritionLog.items).joinedload(
                        NutritionLogItem.ingredient
                    ),
                    joinedload(NutritionLog.meal),
                )
                .where(NutritionLog.id == log_id)
            )
            .unique()
            .first()
        )
        if log is None:
            return _render_index("Log not found"), 404
        ingredients = _all_ingredients(session)
        meals = _all_meals(session)
        return render_template(
            "nutrition_edit_log.html",
            title="Edit Log",
            log=log,
            ingredients=ingredients,
            meals=meals,
        )


@bp.post("/logs/<log_id>/edit")
def edit_log(log_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _make_htmx_response(
                "partials/nutrition_entry_response.html",
                {
                    "recent_logs": [],
                    "ingredients": [],
                    "meals": [],
                    "error": message,
                    "oob": True,
                },
                status=503,
            )
        return _render_index(message), 503

    with SessionLocal() as session:
        log = session.get(NutritionLog, log_id)
        if log is None:
            if _is_htmx():
                return _render_log_response(session, "Log not found", status=404)
            return _render_index("Log not found"), 404

        logged_on_raw = (request.form.get("logged_on") or "").strip()
        time_bucket = (request.form.get("time_bucket") or "").strip().lower()
        meal_id = (request.form.get("meal_id") or "").strip() or None
        notes = (request.form.get("notes") or "").strip() or None

        # Parse date
        if logged_on_raw:
            try:
                logged_on = dt.date.fromisoformat(logged_on_raw)
            except ValueError:
                msg = "Invalid date"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400
        else:
            logged_on = log.logged_on

        # Time bucket
        if time_bucket and time_bucket not in ALLOWED_TIME_BUCKETS:
            msg = "time bucket must be morning, afternoon, or night"
            if _is_htmx():
                return _render_log_response(session, msg, status=400)
            return _render_index(msg), 400
        if not time_bucket:
            time_bucket = log.time_bucket

        # Collect ingredient rows from form
        ingredient_ids = request.form.getlist("ingredient_id")
        servings_raw = request.form.getlist("servings")

        if not ingredient_ids or all(not i.strip() for i in ingredient_ids):
            if meal_id:
                meal = session.get(Meal, meal_id)
                if meal is None:
                    msg = "Unknown meal"
                    if _is_htmx():
                        return _render_log_response(session, msg, status=400)
                    return _render_index(msg), 400
                meal = (
                    session.scalars(
                        select(Meal)
                        .options(joinedload(Meal.ingredients))
                        .where(Meal.id == meal_id)
                    )
                    .unique()
                    .one()
                )
                ingredient_ids = [mi.ingredient_id for mi in meal.ingredients]
                servings_raw = [str(mi.servings) for mi in meal.ingredients]
            else:
                msg = "Add at least one ingredient"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400

        # Validate ingredients and build new items
        new_items: list[NutritionLogItem] = []
        for idx, ing_id in enumerate(ingredient_ids):
            ing_id = ing_id.strip()
            if not ing_id:
                continue
            ingredient = session.get(Ingredient, ing_id)
            if ingredient is None:
                msg = f"Unknown ingredient (row {idx + 1})"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400

            srv_raw = servings_raw[idx].strip() if idx < len(servings_raw) else "1"
            try:
                srv = float(srv_raw) if srv_raw else 1.0
            except ValueError:
                msg = f"Invalid servings for {ingredient.name}"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400
            if srv <= 0:
                msg = f"Servings must be > 0 for {ingredient.name}"
                if _is_htmx():
                    return _render_log_response(session, msg, status=400)
                return _render_index(msg), 400

            new_items.append(
                NutritionLogItem(
                    nutrition_log_id=log_id, ingredient_id=ing_id, servings=srv
                )
            )

        if not new_items:
            msg = "Add at least one ingredient"
            if _is_htmx():
                return _render_log_response(session, msg, status=400)
            return _render_index(msg), 400

        log.logged_on = logged_on
        log.time_bucket = time_bucket
        log.meal_id = meal_id
        log.notes = notes

        # Replace existing log items
        for old_item in list(log.items):
            session.delete(old_item)
        session.flush()

        for item in new_items:
            session.add(item)

        session.commit()

        log_activity("nutrition", "edit_log", f"id={log_id} items={len(new_items)}")

        if _is_htmx():
            return _render_log_response(session, trigger={"nutritionLogSaved": True})
        return redirect(url_for("nutrition.index"))


@bp.post("/logs/<log_id>/delete")
def delete_log(log_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _make_htmx_response(
                "partials/nutrition_entry_response.html",
                {
                    "recent_logs": [],
                    "ingredients": [],
                    "meals": [],
                    "error": message,
                    "oob": True,
                },
                status=503,
            )
        return _render_index(message), 503

    with SessionLocal() as session:
        log = session.get(NutritionLog, log_id)
        if log is None:
            if _is_htmx():
                return _render_log_response(session, "Log not found", status=404)
            return _render_index("Log not found"), 404

        session.delete(log)
        session.commit()

        log_activity("nutrition", "delete_log", f"id={log_id}")

        if _is_htmx():
            return _render_log_response(session)

    return redirect(url_for("nutrition.index"))


# ---------------------------------------------------------------------------
# Ingredient routes
# ---------------------------------------------------------------------------


@bp.get("/ingredients")
def ingredients_index():
    return _render_ingredients()


@bp.post("/ingredients")
def create_ingredient():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _render_ingredient_response(None, message, status=503)
        return _render_ingredients(message), 503

    with SessionLocal() as session:
        name = (request.form.get("name") or "").strip()
        if not name:
            msg = "Ingredient name is required"
            if _is_htmx():
                return _render_ingredient_response(session, msg, status=400)
            return _render_ingredients(msg), 400

        existing = session.scalar(select(Ingredient).where(Ingredient.name == name))
        if existing is not None:
            msg = "An ingredient with that name already exists"
            if _is_htmx():
                return _render_ingredient_response(session, msg, status=400)
            return _render_ingredients(msg), 400

        def _float_field(field: str) -> float | None:
            raw = (request.form.get(field) or "").strip()
            if not raw:
                return None
            return float(raw)

        try:
            ingredient = Ingredient(
                name=name,
                serving_size=(request.form.get("serving_size") or "").strip() or None,
                calories=_float_field("calories"),
                protein_g=_float_field("protein_g"),
                carbs_g=_float_field("carbs_g"),
                fat_g=_float_field("fat_g"),
                fiber_g=_float_field("fiber_g"),
                sugar_g=_float_field("sugar_g"),
                caffeine_mg=_float_field("caffeine_mg"),
            )
        except ValueError:
            msg = "Nutritional values must be numbers"
            if _is_htmx():
                return _render_ingredient_response(session, msg, status=400)
            return _render_ingredients(msg), 400

        session.add(ingredient)
        session.commit()

        log_activity("nutrition", "create_ingredient", f"name={name}")

        if _is_htmx():
            return _render_ingredient_response(
                session, trigger={"ingredientSaved": True}
            )
        return redirect(url_for("nutrition.ingredients_index"))


@bp.get("/ingredients/<ingredient_id>/edit")
def edit_ingredient_form(ingredient_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_ingredients("Database is not configured"), 503

    with SessionLocal() as session:
        ingredient = session.get(Ingredient, ingredient_id)
        if ingredient is None:
            return _render_ingredients("Ingredient not found"), 404
        return render_template(
            "nutrition_edit_ingredient.html",
            title="Edit Ingredient",
            ingredient=ingredient,
        )


@bp.post("/ingredients/<ingredient_id>/edit")
def edit_ingredient(ingredient_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _render_ingredient_response(None, message, status=503)
        return _render_ingredients(message), 503

    with SessionLocal() as session:
        ingredient = session.get(Ingredient, ingredient_id)
        if ingredient is None:
            if _is_htmx():
                return _render_ingredient_response(
                    session, "Ingredient not found", status=404
                )
            return _render_ingredients("Ingredient not found"), 404

        name = (request.form.get("name") or "").strip()
        if not name:
            msg = "Ingredient name is required"
            if _is_htmx():
                return _render_ingredient_response(session, msg, status=400)
            return render_template(
                "nutrition_edit_ingredient.html",
                title="Edit Ingredient",
                ingredient=ingredient,
                error=msg,
            ), 400

        def _float_field(field: str) -> float | None:
            raw = (request.form.get(field) or "").strip()
            if not raw:
                return None
            return float(raw)

        try:
            ingredient.name = name
            ingredient.serving_size = (
                request.form.get("serving_size") or ""
            ).strip() or None
            ingredient.calories = _float_field("calories")
            ingredient.protein_g = _float_field("protein_g")
            ingredient.carbs_g = _float_field("carbs_g")
            ingredient.fat_g = _float_field("fat_g")
            ingredient.fiber_g = _float_field("fiber_g")
            ingredient.sugar_g = _float_field("sugar_g")
            ingredient.caffeine_mg = _float_field("caffeine_mg")
        except ValueError:
            msg = "Nutritional values must be numbers"
            if _is_htmx():
                return _render_ingredient_response(session, msg, status=400)
            return render_template(
                "nutrition_edit_ingredient.html",
                title="Edit Ingredient",
                ingredient=ingredient,
                error=msg,
            ), 400

        session.commit()

        log_activity(
            "nutrition", "edit_ingredient", f"id={ingredient_id} name={ingredient.name}"
        )

        if _is_htmx():
            return _render_ingredient_response(
                session, trigger={"ingredientSaved": True}
            )
        return redirect(url_for("nutrition.ingredients_index"))


@bp.post("/ingredients/fill-missing")
def fill_missing():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _render_ingredient_response(None, message, status=503)
        return _render_ingredients(message), 503

    settings = current_app.config.get("_settings")
    ai_cfg = AiConfig(
        enabled=settings.ai_enabled if settings else False,
        model=settings.anthropic_model if settings else "claude-sonnet-4-20250514",
        api_key=settings.anthropic_api_key if settings else None,
        debug_log=settings.ai_debug_log if settings else False,
    )

    with SessionLocal() as session:
        incomplete = get_ingredients_with_missing_info(session)
        if not incomplete:
            msg = "All ingredients already have complete nutrition info."
            if _is_htmx():
                return _render_ingredient_response(session, msg)
            return _render_ingredients(msg)

        result = fill_missing_ingredient_info(session, ai_cfg, incomplete)

        parts = []
        if result["updated"]:
            parts.append(f"Updated: {', '.join(result['updated'])}")
        if result["failed"]:
            parts.append(f"Failed: {', '.join(result['failed'])}")
        msg = ". ".join(parts) if parts else "No changes made."

        if _is_htmx():
            return _render_ingredient_response(session, msg)
        return _render_ingredients(msg)


@bp.post("/ingredients/<ingredient_id>/delete")
def delete_ingredient(ingredient_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _render_ingredient_response(None, message, status=503)
        return _render_ingredients(message), 503

    with SessionLocal() as session:
        ingredient = session.get(Ingredient, ingredient_id)
        if ingredient is None:
            if _is_htmx():
                return _render_ingredient_response(
                    session, "Ingredient not found", status=404
                )
            return _render_ingredients("Ingredient not found"), 404

        name = ingredient.name
        session.delete(ingredient)
        session.commit()

        log_activity(
            "nutrition", "delete_ingredient", f"id={ingredient_id} name={name}"
        )

        if _is_htmx():
            return _render_ingredient_response(session)

    return redirect(url_for("nutrition.ingredients_index"))


# ---------------------------------------------------------------------------
# Meal routes
# ---------------------------------------------------------------------------


@bp.get("/meals")
def meals_index():
    return _render_meals()


@bp.get("/meals/<meal_id>/json")
def meal_json(meal_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return jsonify({"error": "Database not configured"}), 503

    with SessionLocal() as session:
        meal = (
            session.scalars(
                select(Meal)
                .options(
                    joinedload(Meal.ingredients).joinedload(MealIngredient.ingredient)
                )
                .where(Meal.id == meal_id)
            )
            .unique()
            .first()
        )
        if meal is None:
            return jsonify({"error": "Meal not found"}), 404

        return jsonify(
            {
                "id": meal.id,
                "name": meal.name,
                "ingredients": [
                    {
                        "ingredient_id": mi.ingredient_id,
                        "ingredient_name": mi.ingredient.name,
                        "servings": mi.servings,
                    }
                    for mi in meal.ingredients
                ],
            }
        )


@bp.post("/meals")
def create_meal():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _render_meal_response(None, message, status=503)
        return _render_meals(message), 503

    with SessionLocal() as session:
        name = (request.form.get("name") or "").strip()
        if not name:
            msg = "Meal name is required"
            if _is_htmx():
                return _render_meal_response(session, msg, status=400)
            return _render_meals(msg), 400

        notes = (request.form.get("notes") or "").strip() or None
        ingredient_ids = request.form.getlist("ingredient_id")
        servings_raw = request.form.getlist("servings")

        meal_ingredients: list[MealIngredient] = []
        for idx, ing_id in enumerate(ingredient_ids):
            ing_id = ing_id.strip()
            if not ing_id:
                continue
            ingredient = session.get(Ingredient, ing_id)
            if ingredient is None:
                msg = f"Unknown ingredient (row {idx + 1})"
                if _is_htmx():
                    return _render_meal_response(session, msg, status=400)
                return _render_meals(msg), 400

            srv_raw = servings_raw[idx].strip() if idx < len(servings_raw) else "1"
            try:
                srv = float(srv_raw) if srv_raw else 1.0
            except ValueError:
                msg = f"Invalid servings for {ingredient.name}"
                if _is_htmx():
                    return _render_meal_response(session, msg, status=400)
                return _render_meals(msg), 400

            meal_ingredients.append(MealIngredient(ingredient_id=ing_id, servings=srv))

        if not meal_ingredients:
            msg = "A meal needs at least one ingredient"
            if _is_htmx():
                return _render_meal_response(session, msg, status=400)
            return _render_meals(msg), 400

        meal = Meal(name=name, notes=notes, ingredients=meal_ingredients)
        session.add(meal)
        session.commit()

        log_activity(
            "nutrition", "create_meal", f"name={name} items={len(meal_ingredients)}"
        )

        if _is_htmx():
            return _render_meal_response(session, trigger={"mealSaved": True})
        return redirect(url_for("nutrition.meals_index"))


@bp.get("/meals/<meal_id>/edit")
def edit_meal_form(meal_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return _render_meals("Database is not configured"), 503

    with SessionLocal() as session:
        meal = (
            session.scalars(
                select(Meal)
                .options(
                    joinedload(Meal.ingredients).joinedload(MealIngredient.ingredient)
                )
                .where(Meal.id == meal_id)
            )
            .unique()
            .first()
        )
        if meal is None:
            return _render_meals("Meal not found"), 404
        ingredients = _all_ingredients(session)
        return render_template(
            "nutrition_edit_meal.html",
            title="Edit Meal",
            meal=meal,
            ingredients=ingredients,
        )


@bp.post("/meals/<meal_id>/edit")
def edit_meal(meal_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _render_meal_response(None, message, status=503)
        return _render_meals(message), 503

    with SessionLocal() as session:
        meal = session.get(Meal, meal_id)
        if meal is None:
            if _is_htmx():
                return _render_meal_response(session, "Meal not found", status=404)
            return _render_meals("Meal not found"), 404

        name = (request.form.get("name") or "").strip()
        if not name:
            msg = "Meal name is required"
            if _is_htmx():
                return _render_meal_response(session, msg, status=400)
            return _render_meals(msg), 400

        notes = (request.form.get("notes") or "").strip() or None
        ingredient_ids = request.form.getlist("ingredient_id")
        servings_raw = request.form.getlist("servings")

        new_ingredients: list[MealIngredient] = []
        for idx, ing_id in enumerate(ingredient_ids):
            ing_id = ing_id.strip()
            if not ing_id:
                continue
            ingredient = session.get(Ingredient, ing_id)
            if ingredient is None:
                msg = f"Unknown ingredient (row {idx + 1})"
                if _is_htmx():
                    return _render_meal_response(session, msg, status=400)
                return _render_meals(msg), 400

            srv_raw = servings_raw[idx].strip() if idx < len(servings_raw) else "1"
            try:
                srv = float(srv_raw) if srv_raw else 1.0
            except ValueError:
                msg = f"Invalid servings for {ingredient.name}"
                if _is_htmx():
                    return _render_meal_response(session, msg, status=400)
                return _render_meals(msg), 400

            new_ingredients.append(
                MealIngredient(meal_id=meal_id, ingredient_id=ing_id, servings=srv)
            )

        if not new_ingredients:
            msg = "A meal needs at least one ingredient"
            if _is_htmx():
                return _render_meal_response(session, msg, status=400)
            return _render_meals(msg), 400

        meal.name = name
        meal.notes = notes

        # Replace existing meal ingredients
        for old_mi in list(meal.ingredients):
            session.delete(old_mi)
        session.flush()

        for mi in new_ingredients:
            session.add(mi)

        session.commit()

        log_activity("nutrition", "edit_meal", f"id={meal_id} name={meal.name}")

        if _is_htmx():
            return _render_meal_response(session, trigger={"mealSaved": True})
        return redirect(url_for("nutrition.meals_index"))


@bp.post("/meals/<meal_id>/delete")
def delete_meal(meal_id: str):
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        message = "Database is not configured"
        if _is_htmx():
            return _render_meal_response(None, message, status=503)
        return _render_meals(message), 503

    with SessionLocal() as session:
        meal = session.get(Meal, meal_id)
        if meal is None:
            if _is_htmx():
                return _render_meal_response(session, "Meal not found", status=404)
            return _render_meals("Meal not found"), 404

        name = meal.name
        session.delete(meal)
        session.commit()

        log_activity("nutrition", "delete_meal", f"id={meal_id} name={name}")

        if _is_htmx():
            return _render_meal_response(session)

    return redirect(url_for("nutrition.meals_index"))
