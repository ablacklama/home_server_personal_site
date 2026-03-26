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


def ensure_sqlite_chat_schema(engine) -> None:
    if getattr(engine.dialect, "name", None) != "sqlite":
        return

    with engine.begin() as conn:
        # ai_messages: add conversation_id if missing
        result = conn.execute(text("PRAGMA table_info(ai_messages)"))
        existing = {row[1] for row in result}

        if "conversation_id" not in existing:
            conn.execute(
                text(
                    "ALTER TABLE ai_messages ADD COLUMN conversation_id VARCHAR(36)"
                    " REFERENCES chat_conversations(id)"
                )
            )

        # Make topic nullable (was NOT NULL before).
        # SQLite doesn't support ALTER COLUMN, so we recreate the table.
        result2 = conn.execute(text("PRAGMA table_info(ai_messages)"))
        col_info = {row[1]: row for row in result2}
        topic_col = col_info.get("topic")
        # row format: (cid, name, type, notnull, dflt_value, pk)
        if topic_col and topic_col[3] == 1:  # notnull == 1
            conn.execute(
                text(
                    "CREATE TABLE ai_messages_new ("
                    "  id VARCHAR(36) PRIMARY KEY,"
                    "  topic VARCHAR(160),"
                    "  conversation_id VARCHAR(36) REFERENCES chat_conversations(id),"
                    "  role VARCHAR(16) NOT NULL,"
                    "  content TEXT NOT NULL,"
                    "  ntfy_id VARCHAR(80),"
                    "  meta JSON NOT NULL DEFAULT '{}',"
                    "  created_at DATETIME NOT NULL"
                    ")"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO ai_messages_new"
                    " SELECT id, topic, conversation_id, role, content,"
                    " ntfy_id, meta, created_at FROM ai_messages"
                )
            )
            conn.execute(text("DROP TABLE ai_messages"))
            conn.execute(text("ALTER TABLE ai_messages_new RENAME TO ai_messages"))


def ensure_sqlite_goals_schema(engine) -> None:
    if getattr(engine.dialect, "name", None) != "sqlite":
        return

    with engine.begin() as conn:
        result = conn.execute(text("PRAGMA table_info(user_preferences)"))
        existing = {row[1] for row in result}

        goal_columns = {
            "goal_calories": "FLOAT",
            "goal_protein_g": "FLOAT",
            "goal_carbs_g": "FLOAT",
            "goal_fat_g": "FLOAT",
            "goal_sleep_hours": "FLOAT",
            "goal_workouts_per_week": "INTEGER",
            "goal_caffeine_mg": "FLOAT",
        }

        for col_name, col_type in goal_columns.items():
            if col_name not in existing:
                conn.execute(
                    text(
                        f"ALTER TABLE user_preferences ADD COLUMN {col_name} {col_type}"
                    )
                )
