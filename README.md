# Breakout Scheduler

**Live: [breakouts.princint.ai](https://breakouts.princint.ai)**

A web app to run breakout sessions at retreats, conferences, and unconferences.
People propose topics and rate them 1–5 stars on their phones. An organizer then
generates a schedule (which sessions run, in which room and timeslot) and publishes
it. Built for [PiBBSS](https://pibbss.ai), but general-purpose. One instance hosts
many events, each with its own codes, rooms, and rounds.

## How it works

- **Participants** enter the event code and their name. They propose sessions, rate
  them, and mark the ones they host. After publish, they see their seat for each
  timeslot, with optional push alerts before a session ends.
- **Organizers** use the admin code to open a console. They **Generate** a schedule,
  then pin, swap, move, or remove sessions, and **Publish**. The console shows who is
  left out and why. It also has a Markdown export, a projector view, and a clock for
  each timeslot.
- **Rounds** run one at a time. The live round is the oldest round that is not
  archived. When the organizer archives it, the next round goes live.
- **Solver** (OR-Tools CP-SAT): first it leaves out as few people as possible. Then
  it maximizes interest (★3 = 1, ★4 = 2, ★5 = 3). It obeys room capacity, minimum
  attendance, and host presence.

See [docs/how-it-works.md](docs/how-it-works.md) for full detail.

## Run locally

```bash
uv venv && uv pip install -r pyproject.toml --extra dev
SECRET_KEY=dev DB_PATH=data/app.db .venv/bin/uvicorn main:app --reload
.venv/bin/pytest
```

Or with Docker: `docker compose up --build`, then open http://localhost:8000.

## Deploy

A push to `main` runs CI (tests and Docker build). If CI passes, it SSHes to the VPS
and runs [`scripts/deploy.sh`](scripts/deploy.sh). That script backs up the DB, runs
migrations, rebuilds, and waits until the app is healthy.

First-time VPS setup:

```bash
cp .env.example .env   # set SECRET_KEY, BOOTSTRAP_ADMIN_CODE, SITE_ADDRESS, BASE_URL
docker compose --profile caddy up -d --build
```

Caddy gets HTTPS certificates automatically. Push alerts need HTTPS. Back up the
`breakout-data` volume: it holds the DB, backups, and push keys (`vapid.json`).
