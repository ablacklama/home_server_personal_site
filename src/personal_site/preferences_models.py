from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    report_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    report_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # 0=Monday .. 6=Sunday
    report_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=8)  # 0-23
    report_include: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON list e.g. '["workouts","sleep","nutrition"]'

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )
