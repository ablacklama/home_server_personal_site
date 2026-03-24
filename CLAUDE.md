# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flask-based personal site designed to run on a home server. Tracks workouts, sleep, and caffeine intake. Uses SQLite for storage, HTMX for interactivity, and integrates with ntfy for push notifications. An AI feature (OpenAI) listens to ntfy messages and can log workouts/sleep/caffeine via natural language.

## Commands

- `just sync` — install/sync all deps (including dev group) via uv
- `just dev` — run in debug mode with auto-reload on port 8000
- `just run` — run in production mode
- `just check` — format then lint (ruff format + ruff check)
- `just fix` — auto-fix lint issues and format
- `just lint` — lint only (pass `--fix` via `just lint --fix`)
- `just fmt` — format only
- `uv run pytest` — run tests
- `uv run pytest tests/test_ai_sleep_caffeine.py` — run a specific test file
- `just backup-db` — back up SQLite to S3

## Architecture

**Package structure:** `src/personal_site/` — installed as editable package via uv. Entry point is `personal-site` CLI defined in `pyproject.toml` → `__init__.py:main()`.

**App factory:** `app.py:create_app()` builds the Flask app, initializes the DB, registers blueprints, and spawns background daemon threads (ntfy listener, inactivity watcher).

**Blueprints (each has a `_models.py` companion with SQLAlchemy models):**
- `workouts.py` / `workouts_models.py` — workout types with typed metric schemas (string, integer, hours_minutes) and workout entries logged by date + time bucket (morning/afternoon/night)
- `sleep.py` / `sleep_models.py` — sleep entries with duration and quality
- `caffeine.py` / `caffeine_models.py` — caffeine entries with amount_mg and source

**Key patterns:**
- DB session is stored on `app.session` (a `sessionmaker`), not Flask-SQLAlchemy. Each request opens its own session via `with SessionLocal() as session:`.
- All blueprints support both full-page renders and HTMX partial responses (check `_is_htmx()` → return partial template with optional `HX-Trigger` header).
- Templates use `base.html` layout with HTMX. Partials live in `templates/partials/`.
- `config.py` loads all settings from environment variables (`.env` loaded by just). Settings are a frozen dataclass.

**AI subsystem (`ai.py`):**
- Listens to ntfy topic in background thread, sends incoming messages to OpenAI with tool-calling to log workouts, sleep, or caffeine entries
- Tracks conversation state via `AiPending` / `AiMessage` models for multi-turn follow-up questions
- Uses `_was_sent_by_us()` and tag-based guards to avoid responding to its own messages

**Other modules:**
- `notify.py` — ntfy send/listen helpers
- `security.py` — `@require_admin` decorator (checks `X-Admin-Token` header or `?token=` param)
- `db.py` — SQLAlchemy engine/session setup, SQLite schema migrations via `ensure_sqlite_workouts_schema()`

## Tech Stack

- Python 3.10+, Flask, SQLAlchemy (core, not Flask-SQLAlchemy), SQLite
- uv for package management, just for task running
- ruff for linting and formatting, pytest for tests
- HTMX for frontend interactivity
- ntfy for push notifications, OpenAI API for AI features


## Front-end Aesthetics Guidance

<frontend_aesthetics>
You tend to converge toward generic, "on distribution" outputs. In frontend design, this creates what users call the "AI slop" aesthetic. Avoid this: make creative, distinctive frontends that surprise and delight. Focus on:

Typography: Choose fonts that are beautiful, unique, and interesting. Avoid generic fonts like Arial and Inter; opt instead for distinctive choices that elevate the frontend's aesthetics.

Color & Theme: Commit to a cohesive aesthetic. Use CSS variables for consistency. Dominant colors with sharp accents outperform timid, evenly-distributed palettes. Draw from IDE themes and cultural aesthetics for inspiration.

Motion: Use animations for effects and micro-interactions. Prioritize CSS-only solutions for HTML. Use Motion library for React when available. Focus on high-impact moments: one well-orchestrated page load with staggered reveals (animation-delay) creates more delight than scattered micro-interactions.

Backgrounds: Create atmosphere and depth rather than defaulting to solid colors. Layer CSS gradients, use geometric patterns, or add contextual effects that match the overall aesthetic.

Avoid generic AI-generated aesthetics:

- Overused font families (Inter, Roboto, Arial, system fonts)
- Clichéd color schemes (particularly purple gradients on white backgrounds)
- Predictable layouts and component patterns
- Cookie-cutter design that lacks context-specific character

Interpret creatively and make unexpected choices that feel genuinely designed for the context. Vary between light and dark themes, different fonts, different aesthetics. You still tend to converge on common choices (Space Grotesk, for example) across generations. Avoid this: it is critical that you think outside the box!
</frontend_aesthetics>
