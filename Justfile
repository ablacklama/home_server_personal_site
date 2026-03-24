#!/usr/bin/env just --justfile

set shell := ["zsh", "-cu"]
set dotenv-load := true

APP_HOST := "127.0.0.1"
APP_PORT := "8743"
DOCKER_IMAGE := "personal-site"
DOCKER_CONTAINER := "personal-site"

# choose a task to run
default:
  @just --choose

# Install/sync deps (includes dev group)
sync:
  uv sync --all-groups

# Update lockfile from pyproject
lock:
  uv lock

# Run the app (pass through additional args to the CLI)
run *ARGS:
  uv run personal-site {{ARGS}}

# Run the app in debug mode with reload
dev *ARGS:
  DEBUG=true uv run personal-site --host 0.0.0.0 --port {{APP_PORT}} --debug {{ARGS}}


fix:
  uv run ruff check --fix
  uv run ruff format

fmt:
  uv run ruff format

lint *ARGS:
  uv run ruff check {{ARGS}}

check:
  @just fmt
  @just lint

clean:
  rm -rf .ruff_cache .pytest_cache dist build
  find . -type d -name '__pycache__' -prune -exec rm -rf {} +
  find . -maxdepth 2 -type d -name '*.egg-info' -prune -exec rm -rf {} +

health:
  curl -fsS http://{{APP_HOST}}:{{APP_PORT}}/healthz

# Check if the app is running
status:
  @curl -fsS -o /dev/null http://{{APP_HOST}}:{{APP_PORT}}/healthz && echo "App is running on {{APP_HOST}}:{{APP_PORT}}" || echo "App is not running on {{APP_HOST}}:{{APP_PORT}}"

# Build the Docker image
docker-build:
  docker compose build

# Run the app in Docker (foreground, Ctrl-C to stop)
docker-run *ARGS:
  docker compose up --build {{ARGS}}

# Run the app in Docker (detached)
docker-up *ARGS:
  docker compose up -d --build {{ARGS}}

# Stop the Docker container
docker-down:
  docker compose down

# View Docker container logs
docker-logs *ARGS:
  docker compose logs {{ARGS}}

# Back up SQLite to S3 (runs inside Docker if container is running, else locally)
backup-db:
  @docker compose ps -q personal-site > /dev/null 2>&1 \
    && docker compose exec personal-site uv run python scripts/backup_sqlite_to_s3.py \
    || uv run python scripts/backup_sqlite_to_s3.py

cron-example:
  @echo "# Runs daily at 3:15am"
  @echo "15 3 * * * cd $PWD && just backup-db"

# Uses $ADMIN_TOKEN from your environment.
# Optional message parameter: `just notify-test 'hello'`
notify-test message="Test notification from just":
  curl -fsS -X POST \
    -H "X-Admin-Token: $ADMIN_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"message": "{{message}}"}' \
    http://{{APP_HOST}}:{{APP_PORT}}/admin/notify-test
