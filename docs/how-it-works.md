# How the Breakout Scheduler works

A maintainer/operator reference: how the app behaves, how the schedule is computed,
how to configure it, how to deploy and back it up, and where the code lives. For a
one-minute overview and quickstart, see the [README](../README.md).

## What it's for

Running **breakout sessions** at retreats, conferences, offsites, and unconferences —
in person or online. People propose topics and rate how much they want to attend each
one on their phones; an organizer generates an optimal schedule **minutes before the
block starts** — deciding which sessions run, in which room and timeslot — and
publishes it.

It was built for [PiBBSS](https://pibbss.ai) retreats, where breakouts are proposed
out loud and the organizer has to decide on the spot which sessions run where. Doing
that on a whiteboard ignores three things that matter: how *intensely* people want
each session, room capacity, and the fact that whoever runs a session has to be in
it. This app captures weighted interest in seconds and computes a schedule that
**maximizes interest while making sure almost nobody is left with nothing they'd want
to attend** in any timeslot — and shows the organizer who *would* be left out, so they
can merge or add a session before publishing. It's a general-purpose tool.

A single instance hosts **many events**, each with its own codes, rooms, and rounds.
Within an event the proposal pool churns round to round (retire old, add new) and every
published round is archived.

## Participants

Participants open the event's join link (or enter the event code) and type their name.
Then they:

- **Propose breakouts.** A proposal is just a title by default. Under optional settings
  a proposer can:
  - add a short **description** (only when the organizer has enabled descriptions for the event);
  - set a **maximum** attendance (for an intimate workshop) or a **minimum** below which
    the session shouldn't run.
- **Rate** each proposal **1–5 stars** (1 = won't go … 5 = must). Ratings ≥ 3 count as
  "would attend". New proposals from others stream in without disturbing what they're
  looking at.
- Toggle **"I'm running this"**, which commits them to be present at that session (the
  solver won't double-book a runner).
- See a **personal mobile schedule** once published, with a "→ go here" pick per
  timeslot, and opt into **web-push alerts** that fire shortly before a session ends
  (with a live countdown once a timeslot's clock is started). On iOS, web push only
  works from a home-screen PWA, so the app shows install instructions there.

## Organizers

Organizers enter the admin code and get a dense console:

- A **fairness slider** trading off popularity against coverage (the λ in the model below).
- The live **schedule grid** (timeslots × rooms) with a **Generate** button that runs the solver, and **pin / drop / move /
  place** controls to hand-tune placements and re-solve around the pins.
- A **consequences panel** showing, for the current grid: per-slot **coverage %**, who's
  **left out**, who **found nothing they'd attend**, the share of people who got a
  **most-preferred** session, **capacity overflows**, and the top **unplaced** sessions.
- Per-session settings (min/max attendees), **merge** for duplicate proposals, and
  **edit/delete** of rooms and timeslots.
- A per-event toggle for whether participants may add **descriptions**.
- **Publish**, which freezes the lineup attendees see into a snapshot. Edits made after
  publishing stay private; the console warns that the published schedule has drifted and
  offers a one-click **re-publish** to push the changes live. Regenerating a published
  schedule double-confirms, and **protects sessions whose clock has already started** by
  default.
- **Copy as Markdown** (paste the schedule into Slack/Discord), a **projector view**, and
  **QR codes** for the join link.
- A **Start clock** per timeslot that drives participant countdowns and the pre-end alert.

## The scheduling model

The schedule is computed by an OR-Tools **CP-SAT** solver (`app/solver.py`), built from
the database by a pure function (`app/solver_input.py`). It maximizes:

```
satisfied weighted interest  −  λ · (people left with nothing they'd attend)
```

subject to:

- **Room capacity** is a hard seat cap — the solver never seats more people in a room than
  it holds. Over-subscription is *not* penalized: a popular session still runs, and the
  people who don't fit are routed to their next-best session (overflow is reported, not punished).
- **Per-session max attendees** (host cap) is an additional hard seat cap; the effective
  cap is the tighter of the room and the host's number. Runners always fit.
- **Per-session min attendees** is a hard eligibility gate — a session below the threshold
  of interested people isn't scheduled.
- **Runner presence**: a session's runner can't be in two places at once, and is seated in
  their own session when it runs.
- Each session runs **at most once**, in one room and one timeslot.

The grid the organizer sees is *scored* (not re-solved) on a content-addressed cache, so
the console doesn't re-run the solver unless something actually changed. The full re-solve
only happens when you click **Generate**.

Weighting ramps so enthusiasm dominates: stars map to weights `1–2 → 0` (won't attend),
`3 → 1`, `4 → 4`, `5 → 9`.

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `SECRET_KEY` | dev key | **Set this.** Signs auth + CSRF cookies. `openssl rand -hex 32`. |
| `DB_PATH` | `data/app.db` | SQLite file (on the mounted volume in Docker). |
| `BACKUP_DIR` | `data/backups` | Where periodic online backups are written. |
| `BACKUP_INTERVAL_S` | `300` | Seconds between automatic backups. |
| `BOOTSTRAP_ADMIN_CODE` | unset | If set, required to create the *first* event, then disabled. |
| `COOKIE_SECURE` | `0` | Set to `1` behind a TLS-terminating proxy (e.g. Cloudflare Tunnel) so cookies get the `Secure` flag. |
| `BASE_URL` | derived | Canonical public origin for share links (else derived from the request). |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | auto | Web Push keys (public = base64url, private = PEM). If unset, a keypair is generated and persisted next to the DB. |
| `VAPID_SUBJECT` | `mailto:admin@example.com` | Contact `sub` claim sent with push messages. |
| `VAPID_AUTOGEN` | `1` | Set to `0` to disable auto-generating/persisting push keys. |
| `ALERT_LEAD_MINUTES` | `5` | How many minutes before a slot ends to send the heads-up push. |
| `TRUSTED_PROXIES` | loopback | Extra peer IPs whose `CF-Connecting-IP` is trusted for rate-limiting (Caddy sets this header). |
| `RUN_MIGRATIONS` | unset | Set to `1` to run Alembic on startup (schema changes only). A brand-new DB is stamped at head automatically. |

> **Web Push keys must stay stable.** Rotating the VAPID keypair silently invalidates every
> existing subscription. The default behavior persists a generated keypair to the data volume
> for exactly this reason; in production, set the keys explicitly or keep that file.

## Deploying changes — what's actually true

This is a single container with SQLite on a mounted volume, chosen for operational
simplicity. Be precise about what that buys you:

- **Config changes need no redeploy.** Events, rooms, rounds, timeslots, proposals, and the
  fairness slider are all *data*. Running a retreat — even creating new events — never
  requires rebuilding or restarting the container.
- **Code/template changes are a brief restart**, not zero-downtime. Templates and Python are
  baked into the image, so changing them means rebuild + container restart (~1s; in-flight
  polls drop and htmx retries). The SQLite volume is preserved across restarts.
- **Schema changes are a gated, backed-up step.** They are managed by Alembic and only run
  when you set `RUN_MIGRATIONS=1`. **Back up first**; SQLite table-rebuild migrations are
  slow and lock the DB. Migrations do not run automatically on every boot.
- **True zero-downtime** (rolling restarts) would need two app replicas behind a reverse
  proxy sharing the volume (blue-green). That's deliberately *not* the default for an event
  tool of this size.

### Backup & restore

Automatic backups run every `BACKUP_INTERVAL_S` to `BACKUP_DIR` (WAL-safe `VACUUM INTO`,
last 20 kept). To take one manually or restore:

```bash
# manual backup
docker compose exec app python -c "import sqlite3; sqlite3.connect('/data/app.db').execute('VACUUM INTO \"/data/backups/manual.db\"')"
# restore: stop the app, copy a backup over /data/app.db, remove stale WAL files, start
docker compose stop app
docker compose run --rm app sh -c "cp /data/backups/<file>.db /data/app.db && rm -f /data/app.db-wal /data/app.db-shm"
docker compose up -d app
```

### Schema migration (when you change models)

```bash
# generate a migration after editing app/models.py
DB_PATH=data/app.db python -m alembic revision --autogenerate -m "describe change"
# back up, then apply on deploy by setting RUN_MIGRATIONS=1 in compose and redeploying
```

## Exposing it with Cloudflare Tunnel

To serve the app at a public hostname (e.g. `breakouts.example.com`) without opening any
inbound ports, use Cloudflare Tunnel. **Recommended: token-based, dashboard-managed** — no
`cloudflared login`, no `cert.pem`, no account-wide credentials.

1. Cloudflare **Zero Trust → Networks → Tunnels → Create a tunnel** (Cloudflared). Name it;
   copy the **tunnel token**.
2. In that tunnel's **Public Hostname** tab: add your hostname and point it at service
   `http://app:8000`. (Cloudflare creates the DNS record for you.)
3. Deploy with the token (it stays out of the repo and the image):
   ```bash
   CF_TUNNEL_TOKEN='<token>' docker compose --profile tunnel up -d --build
   ```
   The `tunnel` service reaches `app:8000` over the compose network, so you can drop the
   `ports:` mapping on `app` and expose nothing publicly. Set `COOKIE_SECURE=1` and
   `BASE_URL=https://<your-hostname>` when running behind the tunnel.

**Alternative — locally-managed tunnel (uses `login`):** if you prefer ingress in a config
file, install `cloudflared` on the host and run the interactive auth (this opens a browser
and writes `~/.cloudflared/cert.pem`, bound to your account — it can't be done headlessly):

```bash
cloudflared tunnel login                          # pick the zone in the browser
cloudflared tunnel create breakouts               # writes <UUID>.json credentials
cloudflared tunnel route dns breakouts breakouts.example.com
cloudflared tunnel --url http://localhost:8000 run breakouts   # or a config.yml with ingress
```

Treat `cert.pem` and the credentials JSON as secrets; keep them on the host only.

## Security notes

- Two scopes via signed `HttpOnly`/`SameSite=Lax` cookies (participant vs admin); the admin
  scope is inside the signed payload and can't be forged from a participant code. Put the app
  behind TLS and set `COOKIE_SECURE=1` so `Secure` cookies apply.
- Every action is scoped to the caller's event (no cross-event access).
- CSRF tokens on all state-changing requests; exponential backoff on code entry (guard the
  admin code — it controls the event and the "left out" list).
- **Rating privacy is a UI boundary, not a cryptographic guarantee:** no endpoint exposes
  per-person stars and aggregates are hidden below 3 raters, but an operator with DB access
  can read the raw table. Honest rating is encouraged socially, not enforced.

## Limitations

- On iOS, web-push alerts only work from a **home-screen PWA** (a platform constraint); the
  published grid is the source of truth and the organizer announcing the start is the reliable
  signal.
- Single container / single SQLite writer — sized for retreat scale (dozens of people), not
  thousands of concurrent users.

## Code map

- `app/solver.py`, `app/solver_input.py` — the CP-SAT model and the DB→problem builder; pure
  functions, unit-tested on fixtures (the riskiest logic).
- `app/services/` — rounds, proposals, ratings, push, backup.
- `app/routers/` — `events` (join/create), `participant` (propose/rate/schedule), `admin`
  (console), `push`.
- `app/templates/`, `app/static/` — htmx + Jinja UI.
- `docs/superpowers/` — the original design spec and implementation plan.
</content>
</invoke>
