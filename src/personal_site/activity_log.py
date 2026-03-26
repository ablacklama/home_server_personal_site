"""Structured activity logging — writes to both Python logger and DB."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from flask import current_app, request
from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

logger = logging.getLogger(__name__)


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    timestamp: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
        index=True,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)


def log_activity(
    category: str,
    action: str,
    detail: str | None = None,
) -> None:
    """Log an activity to both Python logging and the DB (best-effort)."""
    ip = None
    path = None
    try:
        ip = request.remote_addr
        path = request.path
    except RuntimeError:
        pass  # outside request context

    logger.info("[%s] %s — %s (ip=%s path=%s)", category, action, detail, ip, path)

    try:
        SessionLocal = current_app.session  # type: ignore[attr-defined]
        if SessionLocal is None:
            return
        with SessionLocal() as session:
            session.add(
                ActivityLog(
                    category=category,
                    action=action,
                    detail=detail,
                    ip=ip,
                    path=path,
                )
            )
            session.commit()
    except Exception:
        logger.debug("Failed to persist activity log", exc_info=True)
