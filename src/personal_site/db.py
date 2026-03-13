from __future__ import annotations

import os
import pathlib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def create_engine_and_sessionmaker(database_url: str):
    connect_args = {}
    if database_url.startswith("sqlite:"):
        connect_args = {"check_same_thread": False}

        # Ensure parent directory exists for sqlite:///./path/to.db
        if ":memory:" not in database_url:
            db_path = database_url.split("sqlite:///", 1)[-1]
            if db_path:
                parent = pathlib.Path(db_path).expanduser().resolve().parent
                os.makedirs(parent, exist_ok=True)

    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, SessionLocal


def ensure_sqlite_workouts_schema(engine) -> None:
    if getattr(engine.dialect, "name", None) != "sqlite":
        return

    with engine.begin() as conn:
        # workout_entries: add performed_on + time_bucket if missing
        columns = conn.execute(text("PRAGMA table_info(workout_entries)"))
        existing = {row[1] for row in columns}

        if "performed_on" not in existing:
            conn.execute(
                text("ALTER TABLE workout_entries ADD COLUMN performed_on DATE")
            )
        if "time_bucket" not in existing:
            conn.execute(
                text("ALTER TABLE workout_entries ADD COLUMN time_bucket VARCHAR(16)")
            )
