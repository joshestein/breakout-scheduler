from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, Depends, Form, HTTPException, Response
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import (Event, Participant, Placement, Proposal, ProposalRunner, Rating,
                        Round, Timeslot, Room, as_utc)
from app.types import INTERESTED_STARS
from app.deps import get_session, require_participant, require_csrf, load_participant
from app.render import render
from app.scoping import owned_proposal
from app.services import proposals as P
from app.services import ratings as RT
from app.services import rounds as RD
from app.routers.events import primary_dest

router = APIRouter()


def _me_event(session: Session, token):
    me = load_participant(session, token)
    if me is None:  # stale/colliding cookie — re-auth
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return me, session.get(Event, me.event_id)


def _active_proposals(session: Session, round_id: int | None) -> list[Proposal]:
    if round_id is None:
        return []
    return list(session.scalars(select(Proposal).where(
        Proposal.round_id == round_id, Proposal.status == "active").order_by(Proposal.id)))


def _rows(session: Session, me: Participant, round_id: int | None) -> list[dict]:
    proposals = _active_proposals(session, round_id)
    my_ratings = {r.proposal_id: r.stars for r in session.scalars(
        select(Rating).where(Rating.participant_id == me.id))}
    my_runner = {pr.proposal_id for pr in session.scalars(
        select(ProposalRunner).where(ProposalRunner.participant_id == me.id))}
    hosts: dict[int, list[str]] = {}
    if proposals:
        for pid, name in session.execute(
                select(ProposalRunner.proposal_id, Participant.display_name)
                .join(Participant, Participant.id == ProposalRunner.participant_id)
                .where(ProposalRunner.proposal_id.in_([p.id for p in proposals]))
                .order_by(ProposalRunner.id)):
            hosts.setdefault(pid, []).append(name)
    rows = []
    for p in proposals:
        interested = session.scalar(select(func.count(Rating.id)).where(
            Rating.proposal_id == p.id, Rating.stars >= INTERESTED_STARS)) or 0
        rows.append({
            "p": p,
            "stars": my_ratings.get(p.id, 0),
            "is_runner": p.id in my_runner,
            "hosts": hosts.get(p.id, []),
            "interested": interested,   # shown regardless of rater count
            "rated": p.id in my_ratings,
            "mine": p.created_by == me.id,   # proposer can edit/delete their own
        })
    rows.sort(key=lambda r: r["p"].id)   # stable creation order (no reorder on refresh)
    return rows


def live_key(session: Session, event_id: int) -> str:
    """The live round and its status, for example "4:published". A page polls with the
    key it was drawn with, so the server can tell when the page is out of date."""
    rnd = RD.live_round(session, event_id)
    return f"{rnd.id}:{rnd.status}" if rnd else "none"


def _go_home(session: Session, event: Event) -> Response:
    """Tell htmx to load the page for the live round: the seat once it is published,
    else propose and rate."""
    return Response(status_code=200, headers={"HX-Redirect": primary_dest(session, event.id)})


@router.get("/app")
def app_home(request: Request, token=Depends(require_participant),
             session: Session = Depends(get_session)):
    me, event = _me_event(session, token)
    published = RD.published_round(session, event.id)
    collecting = RD.input_round(session, event.id)
    rows = _rows(session, me, collecting.id) if collecting else []
    return render(request, "participant.html", me=me, event=event, rows=rows,
                  published=published, collecting=collecting,
                  live_key=live_key(session, event.id))


@router.get("/app/live")
def live_check(k: str = "", page: str = "", token=Depends(require_participant),
               session: Session = Depends(get_session)):
    """Polled by the Rate and Schedule pages with the key they were drawn with (`k`).
    Sends the page on when the organizer publishes, unpublishes or archives. The
    Schedule page (`page=schedule`) also reloads when its schedule changes."""
    me, event = _me_event(session, token)
    if page == "schedule":
        current = schedule_key(session, event.id, _my_schedule(session, event, me)[1])
    else:
        current = live_key(session, event.id)
    if k != current:
        return _go_home(session, event)
    return Response(status_code=204)


@router.post("/propose")
def propose(request: Request, title: str = Form(...), description: str = Form(""),
            min_attendees: int | None = Form(None), max_attendees: int | None = Form(None),
            token=Depends(require_participant), session: Session = Depends(get_session),
            _: None = Depends(require_csrf)):
    me, event = _me_event(session, token)
    rnd = RD.input_round(session, event.id)
    if rnd is None:
        # the round stopped collecting after this page loaded
        return _go_home(session, event)
    if title.strip():
        P.propose(session, event_id=event.id, title=title, created_by=me.id,
                  description=description or None, round_id=rnd.id,
                  min_interest_override=min_attendees, max_attendees=max_attendees)
    rows = _rows(session, me, rnd.id)
    return render(request, "_rate_list.html", me=me, rows=rows)


def _row_response(request: Request, session: Session, me: Participant, event: Event,
                  proposal_id: int):
    """The changed rating row, plus the unrated count (out of band)."""
    rnd = RD.input_round(session, event.id)
    rows = _rows(session, me, rnd.id if rnd else None)
    row = next((r for r in rows if r["p"].id == proposal_id), None)
    if row is None:   # the row is not in the open round: the page is out of date
        return _go_home(session, event)
    unrated_count = sum(1 for r in rows if not r["rated"])
    return render(request, "_rate_row_oob.html", me=me, r=row, rows=rows,
                  unrated_count=unrated_count)


def _in_open_round(session: Session, event: Event, proposal_id: int) -> bool:
    """Ratings and hosts change only in the open round. A closed round's grid is
    fixed, so a vote from an out-of-date page must not change it."""
    rnd = RD.input_round(session, event.id)
    return rnd is not None and session.get(Proposal, proposal_id).round_id == rnd.id


@router.post("/rate")
def rate(request: Request, proposal_id: int = Form(...), stars: int = Form(...),
         token=Depends(require_participant), session: Session = Depends(get_session),
         _: None = Depends(require_csrf)):
    me, event = _me_event(session, token)
    owned_proposal(session, proposal_id, event.id)
    if not _in_open_round(session, event, proposal_id):
        return _go_home(session, event)
    RT.set_rating(session, participant_id=me.id, proposal_id=proposal_id, stars=stars)
    return _row_response(request, session, me, event, proposal_id)


@router.post("/runner-toggle")
def runner_toggle(request: Request, proposal_id: int = Form(...), on: int = Form(...),
                  token=Depends(require_participant), session: Session = Depends(get_session),
                  _: None = Depends(require_csrf)):
    me, event = _me_event(session, token)
    owned_proposal(session, proposal_id, event.id)
    if not _in_open_round(session, event, proposal_id):
        return _go_home(session, event)
    P.set_runner(session, proposal_id, me.id, bool(on))
    if on:  # running implies max interest
        RT.set_rating(session, participant_id=me.id, proposal_id=proposal_id, stars=5)
    return _row_response(request, session, me, event, proposal_id)


def rows_version(rows: list[dict]) -> str:
    """A short hash of everything a rate row shows. The rate page polls with the
    version it was drawn with, and gets a new list only when this changes."""
    shown = [(r["p"].id, r["p"].title, r["p"].description, r["p"].max_attendees,
              r["p"].min_interest_override, r["stars"], r["is_runner"], tuple(r["hosts"]),
              r["mine"]) for r in rows]
    return hashlib.sha1(repr(shown).encode()).hexdigest()[:16]


@router.get("/app/rate-list")
def rate_list(request: Request, v: str = "", token=Depends(require_participant),
              session: Session = Depends(get_session)):
    """Polled by the rate page with the version it was drawn with (`v`). 204 when
    nothing changed, so rows do not move under a finger; else the whole list, so new,
    renamed, removed and newly hosted sessions all show."""
    me, event = _me_event(session, token)
    rnd = RD.input_round(session, event.id)
    rows = _rows(session, me, rnd.id) if rnd else []
    if v == rows_version(rows):
        return Response(status_code=204)
    return render(request, "_rate_list.html", me=me, rows=rows)


def _own_editable(session: Session, me, event, proposal_id: int):
    """Resolve a proposal the caller may edit: theirs, and in the open round."""
    owned_proposal(session, proposal_id, event.id)
    p = session.get(Proposal, proposal_id)
    rnd = RD.input_round(session, event.id)
    if p.created_by != me.id or rnd is None or p.round_id != rnd.id:
        raise HTTPException(status_code=403, detail="can't edit this proposal")
    return p, rnd


@router.post("/propose-edit")
def propose_edit(request: Request, proposal_id: int = Form(...), title: str = Form(...),
                 token=Depends(require_participant), session: Session = Depends(get_session),
                 _: None = Depends(require_csrf)):
    me, event = _me_event(session, token)
    _, rnd = _own_editable(session, me, event, proposal_id)
    if title.strip():
        P.edit_title(session, proposal_id, title)
    return render(request, "_rate_list.html", me=me, rows=_rows(session, me, rnd.id))


@router.post("/propose-delete")
def propose_delete(request: Request, proposal_id: int = Form(...),
                   token=Depends(require_participant), session: Session = Depends(get_session),
                   _: None = Depends(require_csrf)):
    me, event = _me_event(session, token)
    _, rnd = _own_editable(session, me, event, proposal_id)
    RD.remove_session(session, proposal_id)
    return render(request, "_rate_list.html", me=me, rows=_rows(session, me, rnd.id))


def build_published_grid(session: Session, event: Event, rnd: Round,
                         mine: dict[int, int] | None = None) -> list[dict]:
    """The published lineup (the live grid), with clocks and a highlight per slot.
    `mine` (timeslot_id -> proposal_id) is where the solver seated the viewer; without
    a viewer the highlight is the most wanted session (only then are counts needed)."""
    props = {p.id: p for p in session.scalars(
        select(Proposal).where(Proposal.event_id == event.id))}
    rooms = {r.id: r for r in session.scalars(
        select(Room).where(Room.event_id == event.id))}
    ts_by_id = {t.id: t for t in session.scalars(
        select(Timeslot).where(Timeslot.round_id == rnd.id))}
    by_ts: dict[int, list[Placement]] = {}
    if ts_by_id:
        for pl in session.scalars(select(Placement).where(
                Placement.timeslot_id.in_(list(ts_by_id)))):
            by_ts.setdefault(pl.timeslot_id, []).append(pl)
    interest: dict[int, int] = {}
    if mine is None and by_ts:
        placed = {pl.proposal_id for pls in by_ts.values() for pl in pls}
        interest = dict(session.execute(
            select(Rating.proposal_id, func.count(Rating.id))
            .where(Rating.proposal_id.in_(placed), Rating.stars >= INTERESTED_STARS)
            .group_by(Rating.proposal_id)).all())
    grid = []
    for ts in sorted(ts_by_id.values(), key=lambda t: t.position):
        cells = []
        for pl in sorted(by_ts.get(ts.id, []), key=lambda pl: pl.room_id):
            pid, rid = pl.proposal_id, pl.room_id
            room = rooms.get(rid)
            label = room.name if room else "?"
            cells.append({"key": pid, "title": props[pid].title if pid in props else "?",
                          "room_id": rid,
                          "room_label": label, "interested": interest.get(pid, 0),
                          "cap": props[pid].max_attendees if pid in props else None})
        if mine is not None:
            highlight = mine.get(ts.id)
        elif cells:
            highlight = max(cells, key=lambda c: c["interested"])["key"]
        else:
            highlight = None
        grid.append({"ts": ts, "cells": cells, "highlight": highlight})
    return grid


def now_slot_ids(grid: list[dict]) -> set[int]:
    """The timeslots whose clock runs now."""
    now = datetime.now(timezone.utc)
    ids = set()
    for slot in grid:
        ts = slot["ts"]
        if ts.started_at:
            start = as_utc(ts.started_at)
            if start <= now <= start + timedelta(minutes=ts.duration_minutes or 0):
                ids.add(ts.id)
    return ids


def schedule_key(session: Session, event_id: int, grid: list[dict]) -> str:
    """The key a schedule page polls with: the live key, a hash of what the grid
    shows (times, clocks, rooms, titles, highlights) and the slots that run now.
    So a change after publish (a moved session, a new seat, a started clock)
    reloads the page."""
    shown = [(s["ts"].id, s["ts"].name, s["ts"].duration_minutes, str(s["ts"].started_at),
              s["highlight"], [(c["key"], c["title"], c["room_label"], c["cap"])
                               for c in s["cells"]]) for s in grid]
    digest = hashlib.sha1(repr(shown).encode()).hexdigest()[:16]
    now = ",".join(str(i) for i in sorted(now_slot_ids(grid)))
    return f"{live_key(session, event_id)}:{digest}:{now}"


def _my_schedule(session: Session, event: Event, me: Participant) -> tuple[Round | None, list[dict]]:
    rnd = RD.published_round(session, event.id)
    if rnd is None:
        return None, []
    return rnd, build_published_grid(session, event, rnd, RD.assignments_of(session, rnd.id, me.id))


def _public_schedule(session: Session, code: str) -> tuple[Event, Round | None, list[dict]]:
    # participant code only — the public route must not be an admin-code oracle
    event = session.scalar(select(Event).where(Event.participant_code == code))
    if event is None:
        raise HTTPException(status_code=404, detail="unknown event")
    rnd = RD.published_round(session, event.id)
    return event, rnd, build_published_grid(session, event, rnd) if rnd else []


@router.get("/published")
def published(request: Request, token=Depends(require_participant),
              session: Session = Depends(get_session)):
    me, event = _me_event(session, token)
    rnd, grid = _my_schedule(session, event, me)
    return render(request, "published.html", me=me, event=event, rnd=rnd, grid=grid,
                  schedule_md=RD.schedule_markdown(session, rnd.id) if rnd else "",
                  live_key=schedule_key(session, event.id, grid))


@router.get("/present/{code}")
def present(request: Request, code: str, session: Session = Depends(get_session)):
    """Wide, large-type projector view of the published schedule (auto-refreshing)."""
    event, rnd, grid = _public_schedule(session, code)
    return render(request, "present.html", event=event, rnd=rnd, grid=grid,
                  now_ids=now_slot_ids(grid), live_key=schedule_key(session, event.id, grid))


@router.get("/s/{code}")
def public_schedule(request: Request, code: str, session: Session = Depends(get_session)):
    """Read-only published schedule by event code — no sign-in needed (shareable)."""
    event, rnd, grid = _public_schedule(session, code)
    return render(request, "published.html", me=None, event=event, rnd=rnd,
                  grid=grid, public=True, join_code=event.participant_code,
                  schedule_md=RD.schedule_markdown(session, rnd.id) if rnd else "",
                  live_key=schedule_key(session, event.id, grid))


@router.get("/s/{code}/live")
def public_live_check(code: str, k: str = "", session: Session = Depends(get_session)):
    """Polled by the public schedule and the projector view with the key they were
    drawn with (`k`). 204 while it is the same; else the page reloads. The page
    reloads only after this answer, so a short network failure does not leave a
    projector on an error page."""
    event, _, grid = _public_schedule(session, code)
    if k != schedule_key(session, event.id, grid):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return Response(status_code=204)
