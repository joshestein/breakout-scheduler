from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

log = logging.getLogger("breakouts")

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect
from fastapi.templating import Jinja2Templates

from app.config import Settings, load_settings
from app.db import make_engine, make_session_factory
from app.models import Base, as_utc
from app.services.backup import backup_once
from app.ratelimit import Limiter

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _session_alert_loop(app: FastAPI):
    """Push a heads-up a few minutes before a running slot ends (≈ before the next
    session starts). Starts are manual, so 'before it starts' = 'current ending soon'."""
    from sqlalchemy import select
    from app.models import Timeslot, Round
    from app.services import push as PUSH
    from datetime import datetime, timezone, timedelta
    state = app.state

    def scan():
        if not state.settings.push_enabled:
            return
        lead = state.settings.alert_lead_minutes
        s = state.session_factory()
        try:
            now = datetime.now(timezone.utc)
            for ts in s.scalars(select(Timeslot).where(Timeslot.started_at.is_not(None))):
                if ts.id in state.session_alert_sent:
                    continue
                end = as_utc(ts.started_at) + timedelta(minutes=ts.duration_minutes or 0)
                if not (end - timedelta(minutes=lead) <= now < end):
                    continue
                try:
                    rnd = s.get(Round, ts.round_id)
                    if rnd and rnd.status == "published":
                        PUSH.notify_round(state.session_factory, state.settings, ts.round_id,
                                          f"{ts.name} ends in ~{lead} min",
                                          "Wrap up — the next session is starting soon.")
                    # mark sent only AFTER a successful send, so a failed push is
                    # retried next tick instead of being permanently suppressed.
                    state.session_alert_sent.add(ts.id)
                except Exception:
                    log.exception("session alert failed for timeslot %s", ts.id)
        finally:
            s.close()

    async def loop():
        while True:
            try:
                await asyncio.to_thread(scan)
            except Exception:
                log.exception("session alert loop iteration failed")
            await asyncio.sleep(30)
    return loop


def _backup_loop(app: FastAPI):
    state = app.state

    async def loop():
        while True:
            await asyncio.sleep(state.settings.backup_interval_s)
            try:
                if state.settings.db_path != ":memory:" and os.path.exists(state.settings.db_path):
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                    backup_once(state.settings.db_path, state.settings.backup_dir, stamp)
            except Exception:
                log.exception("backup failed")  # don't fail silently — operators rely on this
    return loop


def _alembic_cfg(db_path: str) -> AlembicConfig:
    os.environ["DB_PATH"] = db_path   # alembic/env.py reads the DB location from here
    return AlembicConfig(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini"))


_DEFAULT_SECRET = "dev-insecure-key-change-me"
# Known placeholder keys that must never reach production. A publicly-known signing
# key lets anyone forge an admin cookie. (Length isn't checked here so an existing
# short-but-private deploy key keeps working — rotating it would void live logins.)
_PLACEHOLDER_SECRETS = {_DEFAULT_SECRET, "change-me-to-a-long-random-string"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    # Fail fast if shipped to prod with a known placeholder secret (cookies forgeable)
    if settings.secret_key in _PLACEHOLDER_SECRETS and (settings.cookie_secure or settings.base_url):
        raise RuntimeError("Set SECRET_KEY: refusing to start in production with a placeholder key.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(_session_alert_loop(app)()),
                 asyncio.create_task(_backup_loop(app)())]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()

    app = FastAPI(title="Breakout Scheduler", lifespan=lifespan)

    if settings.db_path not in (":memory:",) and os.path.dirname(settings.db_path):
        os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    engine = make_engine(settings.db_path)
    # Gated migrations: run Alembic only when explicitly asked (after a backup).
    # Otherwise create_all; a brand-new file DB is also stamped at head so later
    # RUN_MIGRATIONS=1 upgrades start from the right version.
    if os.environ.get("RUN_MIGRATIONS") == "1":
        command.upgrade(_alembic_cfg(settings.db_path), "head")
    else:
        fresh = not inspect(engine).get_table_names()
        Base.metadata.create_all(engine)
        if fresh and settings.db_path != ":memory:":
            command.stamp(_alembic_cfg(settings.db_path), "head")

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.limiter = Limiter()
    app.state.session_alert_sent = set()  # timeslot ids already warned (this process)
    app.state.eval_cache = {}  # round_id -> (fingerprint, Result) for the console grid
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    def _utciso(dt):
        # stored datetimes come back from SQLite naive but are UTC; emit an explicit
        # offset so the browser doesn't misparse the countdown as local time.
        return as_utc(dt).isoformat() if dt else ""
    templates.env.filters["utciso"] = _utciso
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from fastapi import Request as _Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(ValueError)
    async def _value_error(_req: _Request, exc: ValueError):
        # service-layer validation errors -> 400, not a raw 500
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    from fastapi.responses import FileResponse

    @app.get("/sw.js")
    def service_worker():
        # served at root so the SW scope covers the whole app
        return FileResponse(os.path.join(STATIC_DIR, "sw.js"),
                            media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    from app.routers import events, participant, admin, push
    app.include_router(events.router)
    app.include_router(participant.router)
    app.include_router(admin.router)
    app.include_router(push.router)

    return app
