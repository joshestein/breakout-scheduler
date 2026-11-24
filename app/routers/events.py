from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import Event, Room, Participant, Proposal
from app.deps import get_session, current_token, require_csrf, participant_cb, load_participant
from app.auth import issue_token
from app.ratelimit import client_ip
from app.render import render
from app.services import rounds as RD

router = APIRouter()


def _set_cookie(resp, token: str, request: Request) -> None:
    secure = request.app.state.settings.cookie_secure or request.url.scheme == "https"
    resp.set_cookie("session", token, httponly=True, samesite="lax",
                    secure=secure, max_age=60 * 60 * 24 * 7, path="/")


def primary_dest(session: Session, event_id: int) -> str:
    """Where a participant should land by default: the schedule once one is
    published and the next round hasn't started collecting, else propose/rate."""
    input_round = RD.input_round(session, event_id)
    collecting_started = input_round is not None and session.scalar(select(func.count(Proposal.id))
        .where(Proposal.round_id == input_round.id, Proposal.status == "active")) > 0
    if collecting_started:
        return "/app"                       # a new round is being proposed/voted
    if RD.published_round(session, event_id) is not None:
        return "/published"                 # schedule is up / ongoing, between rounds
    return "/app"                            # first round: start proposing


@router.get("/")
def landing(request: Request, session: Session = Depends(get_session)):
    tok = current_token(request)
    me = load_participant(session, tok)
    event = session.get(Event, me.event_id) if me else None
    bootstrap_required = (request.app.state.settings.bootstrap_admin_code is not None
                          and not request.app.state.bootstrap_used
                          and session.scalar(select(func.count(Event.id))) == 0)
    return render(request, "landing.html", me=me, event=event,
                  bootstrap_required=bootstrap_required)


@router.post("/enter")
def enter(request: Request, code: str = Form(...), name: str = Form(...),
          session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    limiter = request.app.state.limiter
    # key on the REAL client IP (CF-Connecting-IP behind the tunnel), not ip:code:
    # otherwise each guessed code is a fresh key and brute-forcing is never throttled.
    # A correct entry resets the IP's backoff.
    key = client_ip(request)
    now = time.monotonic()
    if not limiter.allowed(key, now):
        raise HTTPException(status_code=429, detail="too many attempts, slow down")

    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    event = session.scalar(select(Event).where(Event.participant_code == code))
    scope = "participant"
    if event is None:
        event = session.scalar(select(Event).where(Event.admin_code == code))
        scope = "admin"
    if event is None:
        limiter.record_failure(key, now)
        raise HTTPException(status_code=403, detail="invalid code")

    limiter.reset(key)
    # Dedup by name WITHIN ROLE so re-login / multiple devices reuse one identity,
    # but a participant-code entry never matches (or becomes) an admin row — scope
    # comes from the code, and the is_admin partition stops privilege/identity crossover.
    is_admin = scope == "admin"
    me = session.scalar(select(Participant).where(
        Participant.event_id == event.id, Participant.display_name == name,
        Participant.is_admin == is_admin))
    if me is None:
        me = Participant(event_id=event.id, display_name=name, is_admin=is_admin)
        session.add(me); session.flush()

    token = issue_token(me.id, scope, request.app.state.settings.secret_key,
                        cb=participant_cb(me))
    dest = "/admin" if scope == "admin" else primary_dest(session, event.id)
    resp = RedirectResponse(dest, status_code=303)
    _set_cookie(resp, token, request)
    return resp


@router.get("/j/{code}")
def join(request: Request, code: str, session: Session = Depends(get_session)):
    """Shareable join link: pre-fills the event code (just type a name). If the
    visitor is already signed in to this event, go straight to their default view."""
    # join links carry the PARTICIPANT code only (never resolve admin codes here)
    event = session.scalar(select(Event).where(Event.participant_code == code))
    if event is None:
        raise HTTPException(status_code=404, detail="unknown event")
    tok = current_token(request)
    if tok is not None:
        me = load_participant(session, tok)
        if me is not None and me.event_id == event.id:
            return RedirectResponse(primary_dest(session, event.id), status_code=303)
    published = RD.published_round(session, event.id) is not None
    return render(request, "landing.html", me=None, event=None, bootstrap_required=False,
                  prefill_code=code, join_event=event, join_published=published)


@router.post("/events")
def create_event(request: Request, name: str = Form(...),
                 participant_code: str = Form(...), admin_code: str = Form(...),
                 rooms: str = Form(""), admin_name: str = Form("Organizer"),
                 bootstrap_code: str = Form(""),
                 session: Session = Depends(get_session),
                 _: None = Depends(require_csrf)):
    settings = request.app.state.settings
    if (settings.bootstrap_admin_code is not None
            and not request.app.state.bootstrap_used
            and session.scalar(select(func.count(Event.id))) == 0):
        if not hmac.compare_digest(bootstrap_code, settings.bootstrap_admin_code):
            raise HTTPException(status_code=403, detail="invalid bootstrap code")
        request.app.state.bootstrap_used = True
    if participant_code == admin_code:
        raise HTTPException(status_code=400, detail="participant and admin codes must differ")

    event = Event(name=name.strip(), participant_code=participant_code.strip(),
                  admin_code=admin_code.strip())
    session.add(event); session.flush()
    # rooms textarea: "Name, capacity" per line
    for line in rooms.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [x.strip() for x in line.rsplit(",", 1)]
        rname = parts[0]
        cap = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 10
        session.add(Room(event_id=event.id, name=rname, capacity=cap))
    admin = Participant(event_id=event.id, display_name=admin_name.strip() or "Organizer",
                        is_admin=True)
    session.add(admin); session.flush()

    token = issue_token(admin.id, "admin", settings.secret_key, cb=participant_cb(admin))
    resp = RedirectResponse("/admin", status_code=303)
    _set_cookie(resp, token, request)
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session", path="/")
    return resp
