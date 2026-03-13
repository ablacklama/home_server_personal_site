## personal_site

Small Flask site intended to run on a home server. Uses `uv` for dependency management.

### Development

This repo includes a `Justfile` with common dev scripts.

Local environment variables are defined in `.env` (and `just` will load it automatically).

### Pages

- Workouts (mini-site):
	- Dashboard: `GET /workouts/`
	- Add workout type: `GET /workouts/types/new`

Workout types define typed metrics (`string`, `integer`, `hours_minutes`).
Workout entries are logged by day + time bucket (morning/afternoon/night).

- Pick a task interactively: `just`
- Install/sync deps (including dev tools): `just sync`
- Run in debug mode (reload): `just dev`
- Format + lint: `just check`
- Health check: `just health`

### Run (without just)

- `uv run personal-site`
- Or: `uv run personal-site --host 0.0.0.0 --port 8000 --debug`

Health check: `GET /healthz`

### Database backups (S3)

Workouts are stored in SQLite (see `DATABASE_URL` in `.env`).

To back up the SQLite DB to S3:

- Set `BACKUP_S3_PREFIX` in `.env` (example: `s3://my-bucket/personal_site/backups`)
- Run: `just backup-db`

Example cron (daily at 3:15am):

```cron
15 3 * * * cd /path/to/personal_site && just backup-db
```

### Phone notifications (ntfy)

This project includes a simple push notification integration using **ntfy**.

Options:
- Use the public service: `https://ntfy.sh` + the ntfy phone app
- Or self-host ntfy on your network and point `NTFY_BASE_URL` at it

Required env vars:
- `ADMIN_TOKEN` (protects admin endpoints)
- `NOTIFY_ENABLED=true`
- `NTFY_TOPIC=your_topic_name`

Optional env vars:
- `NTFY_BASE_URL=https://ntfy.sh`
- `NTFY_TOKEN=...` (Bearer token)
- `NTFY_USER=...` and `NTFY_PASSWORD=...` (basic auth)

Test endpoint:
- `POST /admin/notify-test`
- Provide admin token via header: `X-Admin-Token: ...` (or `?token=...`)
- Optional JSON body: `{ "message": "hello" }`

Example:

```bash
just dev

curl -X POST \
	-H 'X-Admin-Token: change-me' \
	-H 'Content-Type: application/json' \
	-d '{"message":"hello from curl"}' \
	http://127.0.0.1:8000/admin/notify-test

# or via just:
just notify-test 'hello from just'
```

### Inactivity notifications

If you want a ping when the site hasn't been used for a while:

- `INACTIVITY_NOTIFY_ENABLED=true`
- `INACTIVITY_SECONDS=3600` (default: 1 hour)
- `INACTIVITY_COOLDOWN_SECONDS=21600` (default: 6 hours)

This uses the same ntfy configuration as above.

### AI (ntfy → OpenAI → workout logging)

If enabled, incoming **external** messages on your `NTFY_TOPIC` are sent to OpenAI
and converted into an app action (currently: logging a workout).

Required env vars:

- `AI_ENABLED=true`
- `OPENAI_API_KEY=...`

Optional env vars:

- `OPENAI_MODEL=gpt-5.2`

If a message doesn't include enough information, the app will post exactly one
follow-up question to the same ntfy topic and wait for your next message.
