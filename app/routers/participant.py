from __future__ import annotations

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import (Event, Participant, Proposal, ProposalRunner, Rating, Round,
                        Timeslot, Room)
from app.types import INTERESTED_STARS
from app.deps import get_session, require_participant, require_csrf, load_participant
from app.render import render
from app.scoping import owned_proposal
from app.services import proposals as P
from app.services import ratings as RT
from app.services import rounds as RD

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
        Proposal.round_id == round_id, Proposal.status == "active",
        Proposal.merged_into.is_(None)).order_by(Proposal.id)))


def _latest_round(session: Session, event_id: int, status: str | None = None) -> Round | None:
    q = select(Round).where(Round.event_id == event_id)
    if status:
        q = q.where(Round.status == status)
    return session.scalar(q.order_by(Round.id.desc()))


def _input_round(session: Session, event_id: int) -> Round | None:
    """The round currently open for proposing/rating (draft or collecting)."""
    return session.scalar(select(Round).where(
        Round.event_id == event_id, Round.status.in_(("draft", "collecting"))
    ).order_by(Round.id.desc()))


def _rows(session: Session, me: Participant, round_id: int | None) -> list[dict]:
    proposals = _active_proposals(session, round_id)
    my_ratings = {r.proposal_id: r.stars for r in session.scalars(
        select(Rating).where(Rating.participant_id == me.id))}
    my_runner = {pr.proposal_id for pr in session.scalars(
        select(ProposalRunner).where(ProposalRunner.participant_id == me.id))}
    rows = []
    for p in proposals:
        interested = session.scalar(select(func.count(Rating.id)).where(
            Rating.proposal_id == p.id, Rating.stars >= INTERESTED_STARS)) or 0
        rows.append({
            "p": p,
            "stars": my_ratings.get(p.id, 0),
            "is_runner": p.id in my_runner,
            "interested": interested,   # shown regardless of rater count
            "rated": p.id in my_ratings,
            "mine": p.created_by == me.id,   # proposer can edit/delete their own
        })
    rows.sort(key=lambda r: r["p"].id)   # stable creation order (no reorder on refresh)
    return rows


@router.get("/app")
def app_home(request: Request, token=Depends(require_participant),
             session: Session = Depends(get_session)):
    me, event = _me_event(session, token)
    published = _latest_round(session, event.id, "published")
    collecting = _input_round(session, event.id)
    rows = _rows(session, me, collecting.id) if collecting else []
    return render(request, "participant.html", me=me, event=event,
                  rows=rows, published=published, collecting=collecting)


@router.post("/propose")
def propose(request: Request, title: str = Form(...), description: str = Form(""),
            min_attendees: int | None = Form(None), max_attendees: int | None = Form(None),
            token=Depends(require_participant), session: Session = Depends(get_session),
            _: None = Depends(require_csrf)):
    me, event = _me_event(session, token)
    rnd = _input_round(session, event.id)
    if not event.descriptions_enabled:
        description = ""   # admin disabled descriptions for this event
    if title.strip() and rnd:
        P.propose(session, event_id=event.id, title=title, created_by=me.id,
                  description=description or None, round_id=rnd.id,
                  min_interest_override=min_attendees, max_attendees=max_attendees)
    rows = _rows(session, me, rnd.id) if rnd else []
    return render(request, "_rate_list.html", me=me, rows=rows)


@router.post("/rate")
def rate(request: Request, proposal_id: int = Form(...), stars: int = Form(...),
         token=Depends(require_participant), session: Session = Depends(get_session),
         _: None = Depends(require_csrf)):
    me, event = _me_event(session, token)
    owned_proposal(session, proposal_id, event.id)
    if not 1 <= stars <= 5:
        raise HTTPException(status_code=400, detail="stars must be 1..5")
    RT.set_rating(session, participant_id=me.id, proposal_id=proposal_id, stars=stars)
    rnd = _input_round(session, event.id)
    rows = _rows(session, me, rnd.id if rnd else None)
    row = next(r for r in rows if r["p"].id == proposal_id)
    unrated_count = sum(1 for r in rows if not r["rated"])
    return render(request, "_rate_row_oob.html", me=me, r=row, unrated_count=unrated_count)


@router.post("/runner-toggle")
def runner_toggle(request: Request, proposal_id: int = Form(...), on: int = Form(...),
                  token=Depends(require_participant), session: Session = Depends(get_session),
                  _: None = Depends(require_csrf)):
    me, event = _me_event(session, token)
    owned_proposal(session, proposal_id, event.id)
    P.set_runner(session, proposal_id, me.id, bool(on))
    if on:  # running implies max interest
        RT.set_rating(session, participant_id=me.id, proposal_id=proposal_id, stars=5)
    rnd = _input_round(session, event.id)
    rows = _rows(session, me, rnd.id if rnd else None)
    row = next(r for r in rows if r["p"].id == proposal_id)
    unrated_count = sum(1 for r in rows if not r["rated"])
    return render(request, "_rate_row_oob.html", me=me, r=row, unrated_count=unrated_count)


@router.get("/app/rate-since")
def rate_since(request: Request, after: int = 0, token=Depends(require_participant),
               session: Session = Depends(get_session)):
    """Append-only feed of sessions proposed after `after` (id) — the rate page polls
    this and appends, so existing rows never move (no scroll jumps / misclicks)."""
    me, event = _me_event(session, token)
    rnd = _input_round(session, event.id)
    rows = _rows(session, me, rnd.id) if rnd else []
    new = [r for r in rows if r["p"].id > after]
    unrated_count = sum(1 for r in rows if not r["rated"])
    return render(request, "_rate_since.html", me=me, rows=new, unrated_count=unrated_count)


def _own_editable(session: Session, me, event, proposal_id: int):
    """Resolve a proposal the caller may edit: theirs, and in the open round."""
    owned_proposal(session, proposal_id, event.id)
    p = session.get(Proposal, proposal_id)
    rnd = _input_round(session, event.id)
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
    P.retire(session, proposal_id)
    return render(request, "_rate_list.html", me=me, rows=_rows(session, me, rnd.id))


def build_published_grid(session: Session, event: Event, rnd: Round,
                         my_stars: dict[int, int]) -> list[dict]:
    """Render the PUBLISHED lineup from the frozen snapshot (so post-publish edits
    don't silently change what attendees see), overlaid with LIVE timing
    (started clocks/durations) and per-viewer highlight."""
    import json
    snap = RD.published_snapshot(session, rnd.id)
    if snap is None:
        return []
    entries = json.loads(snap.grid_json)
    props = {p.id: p for p in session.scalars(
        select(Proposal).where(Proposal.event_id == event.id))}
    rooms = {r.id: r for r in session.scalars(
        select(Room).where(Room.event_id == event.id))}
    ts_by_id = {t.id: t for t in session.scalars(
        select(Timeslot).where(Timeslot.round_id == rnd.id))}
    by_ts: dict[int, list] = {}
    for e in entries:
        by_ts.setdefault(e["timeslot_id"], []).append(e)
    grid = []
    for ts in sorted(ts_by_id.values(), key=lambda t: t.position):
        cells = []
        for e in by_ts.get(ts.id, []):
            pid, rid = e["proposal_id"], e["room_id"]
            interested = session.scalar(select(func.count(Rating.id)).where(
                Rating.proposal_id == pid, Rating.stars >= INTERESTED_STARS)) or 0
            room = rooms.get(rid)
            # frozen room name from the snapshot; fall back to the live room
            label = e.get("room") or (room.name if room else "—")
            cells.append({"key": pid, "title": e.get("title"), "room_id": rid,
                          "room_label": label, "my_stars": my_stars.get(pid, 0),
                          "interested": interested,
                          "cap": props[pid].max_attendees if pid in props else None})
        rated = [c for c in cells if c["my_stars"] > 0]
        if rated:
            highlight = max(rated, key=lambda c: c["my_stars"])["key"]
        elif cells:
            highlight = max(cells, key=lambda c: c["interested"])["key"]
        else:
            highlight = None
        grid.append({"ts": ts, "cells": cells, "highlight": highlight,
                     "has_ratings": bool(rated)})
    return grid


@router.get("/published")
def published(request: Request, token=Depends(require_participant),
              session: Session = Depends(get_session)):
    me, event = _me_event(session, token)
    rnd = _latest_round(session, event.id, "published")
    if rnd is None:
        return render(request, "published.html", me=me, event=event, rnd=None, grid=[])
    my_stars = {r.proposal_id: r.stars for r in session.scalars(
        select(Rating).where(Rating.participant_id == me.id))}
    grid = build_published_grid(session, event, rnd, my_stars)
    return render(request, "published.html", me=me, event=event, rnd=rnd, grid=grid,
                  schedule_md=RD.schedule_markdown(session, rnd.id))


@router.get("/present/{code}")
def present(request: Request, code: str, session: Session = Depends(get_session)):
    """Wide, large-type projector view of the published schedule (auto-refreshing)."""
    from datetime import datetime, timezone, timedelta
    event = session.scalar(select(Event).where(Event.participant_code == code))
    if event is None:
        raise HTTPException(status_code=404, detail="unknown event")
    rnd = _latest_round(session, event.id, "published")
    grid = build_published_grid(session, event, rnd, {}) if rnd else []
    now = datetime.now(timezone.utc)
    now_ids = set()
    for slot in grid:
        ts = slot["ts"]
        if ts.started_at:
            start = ts.started_at if ts.started_at.tzinfo else ts.started_at.replace(tzinfo=timezone.utc)
            if start <= now <= start + timedelta(minutes=ts.duration_minutes or 0):
                now_ids.add(ts.id)
    return render(request, "present.html", event=event, rnd=rnd, grid=grid, now_ids=now_ids)


@router.get("/s/{code}")
def public_schedule(request: Request, code: str, session: Session = Depends(get_session)):
    """Read-only published schedule by event code — no sign-in needed (shareable)."""
    # participant code only — the public route must not be an admin-code oracle
    event = session.scalar(select(Event).where(Event.participant_code == code))
    if event is None:
        raise HTTPException(status_code=404, detail="unknown event")
    rnd = _latest_round(session, event.id, "published")
    grid = build_published_grid(session, event, rnd, {}) if rnd else []
    return render(request, "published.html", me=None, event=event, rnd=rnd,
                  grid=grid, public=True, join_code=event.participant_code,
                  schedule_md=RD.schedule_markdown(session, rnd.id) if rnd else "")
