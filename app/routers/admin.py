from __future__ import annotations

from fastapi import APIRouter, Request, Depends, Form, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import (Event, Participant, Proposal, ProposalRunner, Rating, Room,
                        Round, Timeslot, Placement)
from app.types import INTERESTED_STARS, Result
from app.solver_input import Eligibility, eligibility
from app.deps import get_session, require_admin, require_csrf, load_participant
from app.render import render
from app.scoping import (owned_round, owned_proposal, owned_timeslot, owned_placement,
                        owned_room)
from app.services import proposals as P
from app.services import rounds as RD
from app.services import push as PUSH

router = APIRouter(prefix="/admin")


def _share_urls(request: Request, event: Event) -> tuple[str, str]:
    """(join_url, schedule_url). Canonical from BASE_URL config; request-derived fallback."""
    settings = request.app.state.settings
    base = (settings.base_url.rstrip("/") if settings.base_url
            else str(request.base_url).rstrip("/"))
    return f"{base}/j/{event.participant_code}", f"{base}/s/{event.participant_code}"


def _event_of(session: Session, token) -> Event:
    me = load_participant(session, token)
    if me is None:  # stale/colliding cookie — send back to sign-in
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return session.get(Event, me.event_id)


def _current_round(session: Session, event_id: int, round_id: int | None) -> Round | None:
    if round_id:
        return owned_round(session, round_id, event_id)
    # default to the round participants see; else the newest round
    return RD.live_round(session, event_id) or session.scalar(select(Round).where(
        Round.event_id == event_id).order_by(Round.id.desc()))


def _eval_fingerprint(session: Session, rnd: Round) -> tuple:
    """A cheap content hash of everything `evaluate_current` depends on. When it's
    unchanged we reuse the cached Result instead of re-running CP-SAT (the 5s poll
    would otherwise solve continuously). Pinned-state is excluded — it doesn't
    affect a fixed-grid evaluation."""
    ts = session.scalars(select(Timeslot).where(Timeslot.round_id == rnd.id)).all()
    ts_ids = [t.id for t in ts]
    props = session.scalars(select(Proposal).where(Proposal.round_id == rnd.id)).all()
    prop_ids = [p.id for p in props]
    def rows(stmt):
        return tuple(sorted(stmt)) if stmt else ()
    rooms_fp = rows([(r.id, r.capacity) for r in session.scalars(
        select(Room).where(Room.event_id == rnd.event_id))])
    props_fp = rows([(p.id, p.status, p.min_interest_override or -1,
                      p.max_attendees or -1) for p in props])
    pls_fp = rows([(pl.timeslot_id, pl.room_id, pl.proposal_id)
                   for pl in session.scalars(
                       select(Placement).where(Placement.timeslot_id.in_(ts_ids)))] if ts_ids else [])
    ratings_fp = rows([(r.participant_id, r.proposal_id, r.stars) for r in session.scalars(
        select(Rating).where(Rating.proposal_id.in_(prop_ids)))] if prop_ids else [])
    runners_fp = rows([(pr.participant_id, pr.proposal_id) for pr in session.scalars(
        select(ProposalRunner).where(ProposalRunner.proposal_id.in_(prop_ids)))] if prop_ids else [])
    ts_fp = rows([(t.id, t.position, t.duration_minutes) for t in ts])
    return (rnd.min_interest, ts_fp, rooms_fp, props_fp, pls_fp, ratings_fp, runners_fp)


def _evaluate(request: Request, session: Session, rnd: Round) -> Result:
    """`evaluate_current`, cached by a content fingerprint so the 5s poll doesn't
    re-run CP-SAT when nothing relevant changed."""
    fp = _eval_fingerprint(session, rnd)
    cache = request.app.state.eval_cache
    hit = cache.get(rnd.id)
    if hit and hit[0] == fp:
        return hit[1]
    result = RD.evaluate_current(session, rnd.id)
    cache[rnd.id] = (fp, result)
    return result


def _sync_assignments(request: Request, session: Session, event: Event) -> None:
    """Save the seating of every published round, so attendees see where the solver
    put them. Call after each change to a published grid. It uses the same cached
    result as the console, so the console's counts match what attendees see."""
    for rnd in session.scalars(select(Round).where(
            Round.event_id == event.id, Round.status == "published")):
        RD.save_assignments(session, rnd.id, _evaluate(request, session, rnd))


def _session_info(p: Proposal, el: Eligibility, ratings: list[Rating], hosts: set[int],
                  names: dict[int, str]) -> dict:
    """Counts only (no per-person stars): who hosts, how many people besides the
    hosts want it, and how many gave each star level. `interested` and `floor` come
    from the same eligibility code Generate uses."""
    mine = [r for r in ratings if r.proposal_id == p.id]
    guests = [r for r in mine if r.participant_id not in hosts]
    stars = {s: sum(1 for r in guests if r.stars == s) for s in (5, 4, 3)}
    return {
        "p": p,
        "hosts": sorted(names.get(u, "?") for u in hosts),
        "by": names.get(p.created_by),
        "interested": el.interested,
        "stars": stars,
        "n_raters": len(mine),
        "avg": round(sum(r.stars for r in mine) / len(mine), 1) if mine else 0,
        "floor": el.floor,
        "eligible": el.eligible,
    }


def _why_unplaced(x: dict, hosts_of: dict[int, set[int]], placed_at: dict[int, Placement],
                  timeslots: list[Timeslot], titles: dict[int, str],
                  names: dict[int, str]) -> str:
    """One short, always-true reason an active session is not on the grid."""
    if not x["hosts"]:
        return "no host: nobody runs it"
    if x["interested"] < x["floor"]:
        return f'{x["interested"]} want it, needs {x["floor"]}'
    mine = hosts_of.get(x["p"].id, set())
    if mine and timeslots:
        busy_with: list[int] = []
        for ts in timeslots:
            here = [q for q, pl in placed_at.items()
                    if pl.timeslot_id == ts.id and hosts_of.get(q, set()) & mine]
            if not here:
                break
            busy_with += here
        else:
            busy = mine & set().union(*(hosts_of[q] for q in busy_with))
            who = ", ".join(sorted(names.get(u, "?") for u in busy))
            what = ", ".join(sorted({titles[q] for q in busy_with}))
            return f"host busy: {who} runs {what}"
    return f'not picked: {x["interested"]} want it'


def _context(request: Request, session: Session, event: Event, rnd: Round | None) -> dict:
    rooms = list(session.scalars(select(Room).where(Room.event_id == event.id).order_by(Room.id)))
    people = list(session.scalars(select(Participant).where(Participant.event_id == event.id)))
    names = {u.id: u.display_name for u in people}
    join_url, schedule_url = _share_urls(request, event)
    ctx = {"event": event, "rnd": rnd, "rooms": rooms, "pool": [], "removed": [],
           "join_url": join_url, "schedule_url": schedule_url,
           "schedule_md": RD.schedule_markdown(session, rnd.id) if rnd else "",
           "waiting_for": RD.waiting_for(session, rnd) if rnd else None,
           "timeslots": [], "placements": {}, "cells": {}, "unplaced": [],
           "all_rounds": list(session.scalars(
               select(Round).where(Round.event_id == event.id).order_by(Round.id.desc())))}
    if rnd is None:
        return ctx

    # proposals, ratings and hosts are scoped to THIS round (each round is self-contained)
    props = list(session.scalars(select(Proposal).where(
        Proposal.round_id == rnd.id).order_by(Proposal.id)))
    prop_ids = [p.id for p in props]
    titles = {p.id: p.title for p in props}
    ratings = list(session.scalars(select(Rating).where(
        Rating.proposal_id.in_(prop_ids)))) if prop_ids else []
    hosts_of: dict[int, set[int]] = {p.id: set() for p in props}
    if prop_ids:
        for pr in session.scalars(select(ProposalRunner).where(
                ProposalRunner.proposal_id.in_(prop_ids))):
            hosts_of[pr.proposal_id].add(pr.participant_id)

    timeslots = list(session.scalars(select(Timeslot).where(
        Timeslot.round_id == rnd.id).order_by(Timeslot.position)))
    placements: dict[tuple[int, int], Placement] = {}
    placed_at: dict[int, Placement] = {}
    ts_ids = [ts.id for ts in timeslots]
    if ts_ids:
        for pl in session.scalars(select(Placement).where(Placement.timeslot_id.in_(ts_ids))):
            placements[(pl.timeslot_id, pl.room_id)] = pl
            placed_at[pl.proposal_id] = pl

    # stats for the EXACT current grid
    result = _evaluate(request, session, rnd)

    elig = eligibility(session, rnd.id)
    info = {p.id: _session_info(p, elig[p.id], ratings, hosts_of[p.id], names) for p in props}
    active = [info[p.id] for p in props if p.status == "active"]
    room_name = {r.id: r.name for r in rooms}
    ts_name = {ts.id: ts.name for ts in timeslots}
    for x in active:
        pl = placed_at.get(x["p"].id)
        x["where"] = f"{ts_name[pl.timeslot_id]} · {room_name.get(pl.room_id, '?')}" if pl else None
        x["why"] = None if pl else _why_unplaced(x, hosts_of, placed_at, timeslots, titles, names)
    pool = sorted(active, key=lambda x: (-x["interested"], x["p"].id))
    unplaced = [x for x in pool if x["where"] is None]
    removed = [info[p.id] for p in props if p.status != "active"]

    # card for each filled cell: the session's counts plus what the solver seated
    seated = {(c.slot_id, c.candidate_id): c for c in result.placements}
    cells = {}
    for key, pl in placements.items():
        x = info[pl.proposal_id]
        c = seated.get((pl.timeslot_id, pl.proposal_id))
        cells[key] = {**x, "stats": c is not None,
                      "guests": c.seated_guests if c else 0,
                      "capacity": c.capacity if c else 0,
                      "overflow": c.overflow if c else 0,
                      "below_floor": bool(c) and c.seated_guests < x["floor"]}

    # people, not points. Only sessions still in the round count: someone whose only
    # wanted session was removed shows as "haven't rated" or "want nothing", not vanish.
    active_ids = {x["p"].id for x in active}
    admin_ids = {u.id for u in people if u.is_admin}
    host_ids = set().union(*(hosts_of[q] for q in active_ids))
    current = [r for r in ratings if r.proposal_id in active_ids]
    rated_ids = {r.participant_id for r in current}
    wants_ids = {r.participant_id for r in current if r.stars >= INTERESTED_STARS}
    no_interest_ids = rated_ids - wants_ids - host_ids
    not_rated = sorted(u.display_name for u in people
                       if u.id not in admin_ids and u.id not in rated_ids and u.id not in host_ids)
    slots = []
    for ts in timeslots:
        out = result.left_out.get(ts.id, [])
        full = set(result.forced_uncovered.get(ts.id, []))
        slots.append({
            "ts": ts,
            "covered": result.n_present - len(out),
            "full": sorted(names.get(u, "?") for u in out if u in full),
            "nothing": sorted(names.get(u, "?") for u in out if u not in full),
        })

    ctx.update({
        # the scorer finds no solution when the grid breaks a hard rule (e.g. a host in
        # two sessions at once); then there are no honest counts to show
        "grid_ok": result.status in ("OPTIMAL", "FEASIBLE"),
        "timeslots": timeslots, "placements": placements, "cells": cells,
        "pool": pool, "unplaced": unplaced, "removed": removed, "placed_at": placed_at,
        "slots": slots, "n_people": result.n_present + len(no_interest_ids),
        "top_pick": round(result.top_choice_pct * result.n_present / 100),
        "no_interest": sorted(names.get(u, "?") for u in no_interest_ids),
        "not_rated": not_rated, "pin_conflicts": result.pin_conflicts,
        "n_eligible": sum(1 for x in pool if x["eligible"]),
        "n_rated": len(rated_ids),
    })
    return ctx


def _respond(request: Request, session: Session, event: Event, round_id: int):
    """Return the console partial for htmx (no page reload), else a redirect."""
    rnd = owned_round(session, round_id, event.id)
    if request.headers.get("HX-Request"):
        ctx = _context(request, session, event, rnd)
        return render(request, "_console.html", **ctx)
    return RedirectResponse(f"/admin?round_id={round_id}", status_code=303)


def _respond_or_console(request: Request, session: Session, event: Event, round_id: int):
    """Like _respond but tolerates no round (event-level edits like rooms)."""
    if round_id:
        return _respond(request, session, event, round_id)
    rnd = _current_round(session, event.id, None)
    if request.headers.get("HX-Request"):
        ctx = _context(request, session, event, rnd)
        return render(request, "_console.html", **ctx)
    return RedirectResponse("/admin", status_code=303)


@router.get("")
def console(request: Request, round_id: int | None = None,
            token=Depends(require_admin), session: Session = Depends(get_session)):
    event = _event_of(session, token)
    rnd = _current_round(session, event.id, round_id)
    ctx = _context(request, session, event, rnd)
    return render(request, "admin.html", **ctx)


@router.get("/qr")
def join_qr(request: Request, token=Depends(require_admin),
            session: Session = Depends(get_session)):
    event = _event_of(session, token)
    join_url, _ = _share_urls(request, event)
    return render(request, "qr.html", event=event, join_url=join_url)


@router.get("/grid")
def grid(request: Request, round_id: int, token=Depends(require_admin),
         session: Session = Depends(get_session)):
    event = _event_of(session, token)
    rnd = owned_round(session, round_id, event.id)
    ctx = _context(request, session, event, rnd)
    return render(request, "_admin_grid.html", **ctx)


# ---- configuration ---------------------------------------------------------

@router.post("/rooms")
def add_room(request: Request, name: str = Form(...), capacity: int = Form(..., ge=1, le=100000),
             token=Depends(require_admin), session: Session = Depends(get_session),
             _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    session.add(Room(event_id=event.id, name=name.strip(), capacity=capacity))
    session.flush()
    rnd = _current_round(session, event.id, None)
    if rnd is not None:
        return _respond(request, session, event, rnd.id)
    return RedirectResponse("/admin", status_code=303)


@router.post("/room-edit")
def room_edit(request: Request, room_id: int = Form(...), name: str = Form(...),
              capacity: int = Form(..., ge=1, le=100000), round_id: int = Form(0),
              token=Depends(require_admin), session: Session = Depends(get_session),
              _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_room(session, room_id, event.id)
    RD.edit_room(session, room_id, name=name, capacity=capacity)
    _sync_assignments(request, session, event)
    return _respond_or_console(request, session, event, round_id)


@router.post("/room-delete")
def room_delete(request: Request, room_id: int = Form(...), round_id: int = Form(0),
                token=Depends(require_admin), session: Session = Depends(get_session),
                _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_room(session, room_id, event.id)
    RD.delete_room(session, room_id)
    _sync_assignments(request, session, event)
    return _respond_or_console(request, session, event, round_id)


@router.post("/timeslot-edit")
def timeslot_edit(request: Request, round_id: int = Form(...), timeslot_id: int = Form(...),
                  name: str = Form(...), duration_minutes: int = Form(..., ge=1, le=1440),
                  token=Depends(require_admin), session: Session = Depends(get_session),
                  _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    owned_timeslot(session, timeslot_id, event.id)
    RD.edit_timeslot(session, timeslot_id, name=name, duration_minutes=duration_minutes)
    return _respond(request, session, event, round_id)


@router.post("/timeslot-delete")
def timeslot_delete(request: Request, round_id: int = Form(...), timeslot_id: int = Form(...),
                    token=Depends(require_admin), session: Session = Depends(get_session),
                    _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    owned_timeslot(session, timeslot_id, event.id)
    RD.delete_timeslot(session, timeslot_id)
    _sync_assignments(request, session, event)
    return _respond(request, session, event, round_id)


@router.post("/proposal-edit")
def proposal_edit(request: Request, round_id: int = Form(...), proposal_id: int = Form(...),
                  title: str = Form(...), token=Depends(require_admin),
                  session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    owned_proposal(session, proposal_id, event.id)
    if title.strip():
        P.edit_title(session, proposal_id, title)
    return _respond(request, session, event, round_id)


@router.post("/round")
def add_round(request: Request, name: str = Form(...),
              min_interest: int = Form(2, ge=1), token=Depends(require_admin),
              session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    rnd = RD.create_round(session, event_id=event.id, name=name.strip(),
                          min_interest=min_interest)
    session.flush()
    return _respond(request, session, event, rnd.id)


@router.post("/timeslot")
def add_timeslot(request: Request, round_id: int = Form(...), name: str = Form(...),
                 duration_minutes: int = Form(45, ge=1, le=1440),
                 token=Depends(require_admin), session: Session = Depends(get_session),
                 _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    pos = session.scalar(select(func.count(Timeslot.id)).where(
        Timeslot.round_id == round_id)) or 0
    RD.add_timeslot(session, round_id=round_id, name=name.strip(), position=pos,
                    duration_minutes=duration_minutes)
    return _respond(request, session, event, round_id)


@router.post("/add-session")
def add_session(request: Request, round_id: int = Form(...), title: str = Form(...),
                token=Depends(require_admin), session: Session = Depends(get_session),
                _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    me = session.get(Participant, token.pid)
    if title.strip():
        # organizer-added topic: created_by is the organizer, but they are NOT
        # auto-added as a runner (it's a topic on behalf of the room).
        P.propose(session, event_id=event.id, title=title, created_by=me.id,
                  runner=False, round_id=round_id)
    return _respond(request, session, event, round_id)


@router.post("/round-settings")
def round_settings(request: Request, round_id: int = Form(...),
                   min_interest: int = Form(..., ge=1),
                   token=Depends(require_admin), session: Session = Depends(get_session),
                   _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    rnd = owned_round(session, round_id, event.id)
    rnd.min_interest = min_interest
    session.flush()
    return _respond(request, session, event, round_id)


# ---- solve & grid editing --------------------------------------------------

@router.post("/solve")
def solve(request: Request, round_id: int = Form(...), pin_started: int = Form(1),
          token=Depends(require_admin), session: Session = Depends(get_session),
          _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    # protect sessions already in progress: pin them so regenerate keeps them put
    # (unless the admin explicitly chose to re-solve everything).
    if pin_started:
        RD.pin_started_placements(session, round_id)
    result = RD.solve_round(session, round_id)
    try:
        RD.apply_result(session, round_id, result)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _sync_assignments(request, session, event)
    return _respond(request, session, event, round_id)


@router.post("/pin")
def pin(request: Request, round_id: int = Form(...), placement_id: int = Form(...),
        pinned: int = Form(...), token=Depends(require_admin),
        session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_placement(session, placement_id, event.id)
    RD.set_pin(session, placement_id, bool(pinned))
    return _respond(request, session, event, round_id)


@router.post("/force-place")
def force_place(request: Request, round_id: int = Form(...), timeslot_id: int = Form(...),
                room_id: int = Form(...), proposal_id: int = Form(...),
                token=Depends(require_admin), session: Session = Depends(get_session),
                _: None = Depends(require_csrf)):
    """Put a session in a cell by hand (pinned). Works on a filled cell (the old
    session goes back to the pool) and on a placed session (it moves)."""
    event = _event_of(session, token)
    owned_timeslot(session, timeslot_id, event.id)
    prop = owned_proposal(session, proposal_id, event.id)
    owned_room(session, room_id, event.id)
    _same_round(session, timeslot_id, proposal_id)
    if prop.status != "active":
        raise HTTPException(status_code=400, detail="This session was removed. Restore it first.")
    try:
        RD.place(session, timeslot_id=timeslot_id, room_id=room_id, proposal_id=proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _sync_assignments(request, session, event)
    return _respond(request, session, event, round_id)


def _same_round(session: Session, timeslot_id: int, proposal_id: int) -> None:
    """Guard: a placement must keep a proposal within its own round's grid."""
    ts = session.get(Timeslot, timeslot_id)
    pr = session.get(Proposal, proposal_id)
    if ts is None or pr is None or ts.round_id != pr.round_id:
        raise HTTPException(status_code=400, detail="proposal and timeslot are in different rounds")


# ---- proposal management ----------------------------------------------------

@router.post("/remove")
def remove(request: Request, round_id: int = Form(...), proposal_id: int = Form(...),
           token=Depends(require_admin), session: Session = Depends(get_session),
           _: None = Depends(require_csrf)):
    """Take a session out of the round: off the grid, and Generate won't use it."""
    event = _event_of(session, token)
    owned_proposal(session, proposal_id, event.id)
    RD.remove_session(session, proposal_id)
    _sync_assignments(request, session, event)
    return _respond(request, session, event, round_id)


@router.post("/restore")
def restore(request: Request, round_id: int = Form(...), proposal_id: int = Form(...),
            token=Depends(require_admin), session: Session = Depends(get_session),
            _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_proposal(session, proposal_id, event.id)
    P.reactivate(session, proposal_id)
    return _respond(request, session, event, round_id)


# ---- publish / clocks / archive --------------------------------------------

@router.post("/publish")
def publish(request: Request, round_id: int = Form(...), token=Depends(require_admin),
            session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    RD.publish(session, round_id)
    _sync_assignments(request, session, event)
    return _respond(request, session, event, round_id)


@router.post("/unpublish")
def unpublish(request: Request, round_id: int = Form(...), token=Depends(require_admin),
              session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    RD.unpublish(session, round_id)
    return _respond(request, session, event, round_id)


@router.post("/round-archive")
def round_archive(request: Request, round_id: int = Form(...), token=Depends(require_admin),
                  session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    RD.archive(session, round_id)
    # go on to the round that is live now, if there is one
    live = RD.live_round(session, event.id)
    return _respond(request, session, event, live.id if live else round_id)


@router.post("/round-restore")
def round_restore(request: Request, round_id: int = Form(...), token=Depends(require_admin),
                  session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    RD.restore(session, round_id)
    _sync_assignments(request, session, event)
    return _respond(request, session, event, round_id)


@router.post("/timeslot-start")
def timeslot_start(request: Request, background: BackgroundTasks,
                   round_id: int = Form(...), timeslot_id: int = Form(...),
                   token=Depends(require_admin), session: Session = Depends(get_session),
                   _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    ts = owned_timeslot(session, timeslot_id, event.id)
    RD.start_timeslot(session, timeslot_id)
    # push "go to your session" alert to subscribers (after the response, off-thread)
    background.add_task(PUSH.notify_round, request.app.state.session_factory,
                        request.app.state.settings, round_id,
                        f"{ts.name} is starting", "Open the schedule for your session.")
    return _respond(request, session, event, round_id)


