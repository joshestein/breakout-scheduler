# Breakout Scheduler

**Live: [breakouts.assistant.ms](https://breakouts.assistant.ms)**

A small web app for running **breakout sessions** at retreats, conferences,
offsites, and unconferences — in person or online. People propose topics and rate
how much they want to attend each one on their phones; an organizer then generates an
optimal schedule **minutes before the block starts** — which sessions run, in which
room and timeslot — and publishes it. Built for [PiBBSS](https://pibbss.ai), but
general-purpose.

The schedule (an OR-Tools CP-SAT solver) first leaves as few people as possible with
nothing they'd attend, then seats people in the sessions they want most. It respects
room capacity, runs a session only if enough people besides its host come, and keeps
each session's runner free to be in it. A single instance
hosts many events, each with its own codes, rooms, and rounds.

## How it works

- **Participants** enter an event code + their name, propose breakouts, give each a
  **1–5 star** rating, and toggle **"I'm running this"** (which means they must be
  present at it). A proposal is just a title by default; under optional settings a
  proposer can add a short description, cap or floor
  attendance (a max for an intimate workshop, a min below which it shouldn't run).
  They get a mobile schedule with their personal
  "→ go here" pick per timeslot, plus optional **web-push alerts** before a session
  ends (with a countdown once a timeslot's clock is running).
- **Organizers** enter the admin code and get a console with the live schedule grid.
  Each card says who hosts it, how many are seated, and how many want it. Each session
  not on the grid says why. A side panel counts people: how many have a session in
  each slot, who is left out and why, and who has not rated yet. They **Generate** a
  schedule, then pin, swap, move, or remove sessions by hand (a removed session stays
  out until restored), and **Publish**. Attendees then see the live grid. The schedule
  can be copied as **Markdown** for Slack/Discord or opened as a **projector view**,
  and each timeslot has a **Start clock** that drives participant countdowns and
  pre-end alerts.
- The solver minimizes people left out, then maximizes interest points (★3 = 1, ★4 = 2,
  ★5 = 3; hosts get none for their own session), then prefers the earlier proposal. It
  respects room capacity (a hard seat cap; over-subscription is routed to next-best
  sessions), a minimum number of non-host attendees, and runner presence. Every room
  is used in every timeslot. The proposal pool churns round to round.

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
