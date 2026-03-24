from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Ingredient(Base):
    __tablename__ = "ingredients"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    serving_size: Mapped[str | None] = mapped_column(String(80), nullable=True)
    calories: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbs_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    fiber_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    sugar_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    caffeine_mg: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )


class Meal(Base):
    __tablename__ = "meals"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )

    ingredients: Mapped[list[MealIngredient]] = relationship(
        back_populates="meal", cascade="all, delete-orphan"
    )


class MealIngredient(Base):
    __tablename__ = "meal_ingredients"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    meal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("meals.id"), nullable=False
    )
    ingredient_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ingredients.id"), nullable=False
    )
    servings: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    meal: Mapped[Meal] = relationship(back_populates="ingredients")
    ingredient: Mapped[Ingredient] = relationship()


class NutritionLog(Base):
    __tablename__ = "nutrition_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    logged_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    time_bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    meal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("meals.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )

    meal: Mapped[Meal | None] = relationship()
    items: Mapped[list[NutritionLogItem]] = relationship(
        back_populates="nutrition_log", cascade="all, delete-orphan"
    )

    @property
    def total_calories(self) -> float:
        return sum((it.ingredient.calories or 0) * it.servings for it in self.items)

    @property
    def total_protein_g(self) -> float:
        return sum((it.ingredient.protein_g or 0) * it.servings for it in self.items)

    @property
    def total_carbs_g(self) -> float:
        return sum((it.ingredient.carbs_g or 0) * it.servings for it in self.items)

    @property
    def total_fat_g(self) -> float:
        return sum((it.ingredient.fat_g or 0) * it.servings for it in self.items)

    @property
    def total_sugar_g(self) -> float:
        return sum((it.ingredient.sugar_g or 0) * it.servings for it in self.items)

    @property
    def total_caffeine_mg(self) -> float:
        return sum((it.ingredient.caffeine_mg or 0) * it.servings for it in self.items)


class NutritionLogItem(Base):
    __tablename__ = "nutrition_log_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    nutrition_log_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("nutrition_logs.id"), nullable=False
    )
    ingredient_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ingredients.id"), nullable=False
    )
    servings: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    nutrition_log: Mapped[NutritionLog] = relationship(back_populates="items")
    ingredient: Mapped[Ingredient] = relationship()
