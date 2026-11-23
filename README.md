# Breakout Scheduler

**Live: [breakouts.assistant.ms](https://breakouts.assistant.ms)**

A small web app for running **breakout sessions** at retreats, conferences,
offsites, and unconferences — in person or online. People propose topics and rate
how much they want to attend each one on their phones; an organizer then generates an
optimal schedule **minutes before the block starts** — which sessions run, in which
room and timeslot — and publishes it. Built for [PiBBSS](https://pibbss.ai), but
general-purpose.

The schedule (an OR-Tools CP-SAT solver) maximizes how much people want the sessions
that run while making sure almost nobody is left with nothing they'd attend, respects
room capacity, and keeps each session's runner free to be in it. A single instance
hosts many events, each with its own codes, rooms, and rounds.

## How it works

- **Participants** enter an event code + their name, propose breakouts, give each a
  **1–5 star** rating, and toggle **"I'm running this"** (which means they must be
  present at it). A proposal is just a title by default; under optional settings a
  proposer can add a short description (if the organizer allows it), cap or floor
  attendance (a max for an intimate workshop, a min below which it shouldn't run).
  They get a mobile schedule with their personal
  "→ go here" pick per timeslot, plus optional **web-push alerts** before a session
  ends (with a countdown once a timeslot's clock is running).
- **Organizers** enter the admin code and get a dense console: a fairness slider
  (popularity ↔ coverage), the live schedule grid, and a "consequences" panel showing per-slot coverage %, who's
  left out, who found nothing they'd attend, the share who got a most-preferred
  session, capacity overflows, and the top unplaced sessions. They **Generate** a
  schedule, pin/drop/move placements and re-solve around pins, tune per-session
  settings, then **Publish**, which freezes the lineup
  attendees see (later edits stay private until you re-publish). The schedule can be
  copied as **Markdown** for Slack/Discord or opened as a **projector view**, and each
  timeslot has a **Start clock** that drives participant countdowns and pre-end alerts.
- The solver balances `satisfied weighted interest − λ·(people left out)` subject to
  room capacity (a hard seat cap; over-subscription is routed to next-best sessions, not punished)
  and runner-presence constraints. The proposal pool churns round to round, and every
  published round is archived.

## Quickstart (Docker)

```bash
# 1. Set a real secret in docker-compose.yml (SECRET_KEY: `openssl rand -hex 32`), then:
docker compose up --build
# 2. Open http://localhost:8000 and create an event (name, participant code,
#    admin code, rooms). You're dropped into the organizer console.
# 3. Share the participant code; share the admin code only with organizers.
```

### Deploy on a VPS (Caddy + HTTPS)

```bash
# On a VPS with Docker, ports 80 + 443 open:
cp .env.example .env   # fill in SECRET_KEY, BOOTSTRAP_ADMIN_CODE, SITE_ADDRESS, BASE_URL
docker compose --profile caddy up -d --build
```

Point an `A` record for `SITE_ADDRESS` at the VPS. Before DNS is ready, use
`breakouts.<ip-with-dashes>.sslip.io` (e.g. `breakouts.203-0-113-7.sslip.io`) to get
real HTTPS, which service workers and push alerts need. Back up the `breakout-data`
volume: it holds the DB and the push keys (`vapid.json`).

### Run locally without Docker

```bash
pip install -e ".[dev]"
SECRET_KEY=dev DB_PATH=data/app.db uvicorn main:app --reload
pytest        # tests
```
