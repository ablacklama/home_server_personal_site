from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class WorkoutType(Base):
    __tablename__ = "workout_types"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    metric_schema: Mapped[list[dict]] = mapped_column(
        JSON, nullable=False, default=list
    )
    calories_per_hour: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )

    entries: Mapped[list["WorkoutEntry"]] = relationship(
        back_populates="workout_type", cascade="all, delete"
    )


class WorkoutEntry(Base):
    __tablename__ = "workout_entries"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workout_type_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workout_types.id"), nullable=False
    )
    performed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )

    performed_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    time_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    workout_type: Mapped[WorkoutType] = relationship(back_populates="entries")
