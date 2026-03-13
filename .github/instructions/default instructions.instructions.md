---
description: Describe when these instructions should be loaded
applyTo: '**'
---
# Personal Site architecture notes (for Copilot)

## Mental model

This repo is a **collection of small “sites”** inside one Flask app, with
HTMX-powered interactions for data-heavy pages.

- The root page (`/`) is the **main site**.
- Each section (e.g. Workouts) is its own **mini-site** with:
  - a Flask Blueprint under `src/personal_site/<section>.py`
  - templates that share a section base template (e.g. `workouts_base.html`)
  - multiple pages under one URL prefix (`/workouts/...`)

When adding a new section, prefer creating a new blueprint instead of mixing routes into `app.py`.

## Folder conventions

- App entrypoint: `src/personal_site/__init__.py` (`personal-site` CLI)
- Flask app factory: `src/personal_site/app.py`
- Section blueprints: `src/personal_site/*.py` (e.g. `workouts.py`)
- Templates: `src/personal_site/templates/`
  - top-level `base.html` is the global layout
  - section base templates (e.g. `workouts_base.html`) extend `base.html`
  - shared HTMX fragments live in `templates/partials/`
- Static assets: `src/personal_site/static/`
  - `static/js/site.js` wires theme switching + HTMX success events
  - `static/css/site.css` holds the theme token system

## Dynamic UI (HTMX)

- Use HTMX for create/delete flows so updates happen without full reloads.
- Routes detect `HX-Request` and return partial templates with `hx-swap-oob`.
- For non-HTMX requests, keep standard redirects or full-page renders.

## Data storage

- Default DB is SQLite via `DATABASE_URL` (see `.env`).
- SQLAlchemy models live near their section code (e.g. `workouts_models.py`).
- Prefer keeping the DB schema flexible with JSON columns for “unknown future metrics”, but validate/cast on input.

SQLite migrations: this project uses lightweight, ad-hoc schema patching for early-stage evolution.

## Workouts section guidelines

- Workout types define a `metric_schema`: list of `{key, type}`.
- Supported metric types:
  - `string`
  - `integer`
  - `hours_minutes` (stored as `{hours, minutes}`)
- Workout entries use a day + time bucket UX:
  - date = `performed_on`
  - time bucket = `morning` / `afternoon` / `night`

## Themes + styling

- Theme tokens live on `:root[data-theme="..."]` in `static/css/site.css`.
- Default theme is `ember`; additional themes are defined and selectable.
- Theme preference is persisted in `localStorage` by `static/js/site.js`.

## Dev workflows

- Dependency management: `uv`
  - `uv sync --all-groups`
  - `uv lock`
- Commands: `just`
  - `just` to choose a task
  - `just dev` to run locally
  - `just check` for formatting + linting

## Backups

- SQLite is backed up to S3 via `scripts/backup_sqlite_to_s3.py`.
- Use cron to run daily:
  - `just cron-example` prints a starting point.
