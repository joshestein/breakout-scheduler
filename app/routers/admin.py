from __future__ import annotations

from fastapi import APIRouter, Request, Depends, Form, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import (Event, Participant, Proposal, ProposalRunner, Rating, Room,
                        Round, RoundParticipant, Timeslot, TimeslotRoom, Placement,
                        RoundSnapshot)
from app.types import INTERESTED_STARS
from app.deps import get_session, require_admin, require_csrf, load_participant
from app.render import render
from app.scoping import (owned_round, owned_proposal, owned_timeslot, owned_placement,
                        owned_participant, owned_room, room_in_timeslot)
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
    return session.scalar(select(Round).where(
        Round.event_id == event_id, Round.status != "archived").order_by(Round.id.desc()))


def _agg(session: Session, proposal_id: int) -> dict:
    rows = session.scalars(select(Rating).where(Rating.proposal_id == proposal_id)).all()
    hist = [0, 0, 0, 0, 0]
    for r in rows:
        hist[r.stars - 1] += 1
    interested = sum(h for s, h in zip(range(1, 6), hist) if s >= INTERESTED_STARS)
    avg = round(sum(r.stars for r in rows) / len(rows), 1) if rows else 0
    # interest is shown to the organizer regardless of rater count (per request)
    return {"interested": interested, "avg": avg, "hist": hist,
            "n_raters": len(rows), "suppressed": False}


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
    tr = rows([(x.timeslot_id, x.room_id) for x in session.scalars(
        select(TimeslotRoom).where(TimeslotRoom.timeslot_id.in_(ts_ids)))] if ts_ids else [])
    rooms_fp = rows([(r.id, r.capacity) for r in session.scalars(
        select(Room).where(Room.event_id == rnd.event_id))])
    props_fp = rows([(p.id, p.status, p.merged_into or 0, p.min_interest_override or -1,
                      p.max_attendees or -1) for p in props])
    pls_fp = rows([(pl.timeslot_id, pl.room_id, pl.proposal_id)
                   for pl in session.scalars(
                       select(Placement).where(Placement.timeslot_id.in_(ts_ids)))] if ts_ids else [])
    ratings_fp = rows([(r.participant_id, r.proposal_id, r.stars) for r in session.scalars(
        select(Rating).where(Rating.proposal_id.in_(prop_ids)))] if prop_ids else [])
    runners_fp = rows([(pr.participant_id, pr.proposal_id) for pr in session.scalars(
        select(ProposalRunner).where(ProposalRunner.proposal_id.in_(prop_ids)))] if prop_ids else [])
    present_fp = rows([(x.participant_id, x.present) for x in session.scalars(
        select(RoundParticipant).where(RoundParticipant.round_id == rnd.id))])
    ts_fp = rows([(t.id, t.position, t.duration_minutes) for t in ts])
    return (rnd.lambda_fairness, rnd.min_interest, ts_fp, tr, rooms_fp,
            props_fp, pls_fp, ratings_fp, runners_fp, present_fp)


def _context(request: Request, session: Session, event: Event, rnd: Round | None) -> dict:
    rooms = list(session.scalars(select(Room).where(Room.event_id == event.id).order_by(Room.id)))
    # pool is scoped to THIS round (each round is self-contained)
    proposals = [] if rnd is None else list(session.scalars(select(Proposal).where(
        Proposal.round_id == rnd.id, Proposal.status == "active",
        Proposal.merged_into.is_(None)).order_by(Proposal.id)))
    titles = {p.id: p.title for p in session.scalars(
        select(Proposal).where(Proposal.event_id == event.id))}
    names = {u.id: u.display_name for u in session.scalars(
        select(Participant).where(Participant.event_id == event.id))}

    pool = [{"p": p, "by": names.get(p.created_by), **_agg(session, p.id)}
            for p in proposals]
    pool.sort(key=lambda x: (-x["interested"], x["p"].id))

    join_url, schedule_url = _share_urls(request, event)
    schedule_md = RD.schedule_markdown(session, rnd.id) if rnd else ""

    ctx = {"event": event, "rnd": rnd, "rooms": rooms, "pool": pool,
           "join_url": join_url, "schedule_url": schedule_url, "schedule_md": schedule_md,
           "titles": titles, "names": names, "participants": [],
           "timeslots": [], "grid": {}, "consequences": None, "placed_ids": set(),
           "unplaced": [], "all_rounds": list(session.scalars(
               select(Round).where(Round.event_id == event.id).order_by(Round.id.desc())))}
    if rnd is None:
        return ctx

    timeslots = list(session.scalars(select(Timeslot).where(
        Timeslot.round_id == rnd.id).order_by(Timeslot.position)))
    ts_rooms = {ts.id: {tr.room_id for tr in session.scalars(
        select(TimeslotRoom).where(TimeslotRoom.timeslot_id == ts.id))} for ts in timeslots}
    placements = {}
    placed_ids = set()
    for ts in timeslots:
        for pl in session.scalars(select(Placement).where(Placement.timeslot_id == ts.id)):
            placements[(ts.id, pl.room_id)] = pl
            placed_ids.add(pl.proposal_id)

    # presence
    rp = {x.participant_id: x.present for x in session.scalars(
        select(RoundParticipant).where(RoundParticipant.round_id == rnd.id))}
    participants = [{"u": u, "present": rp.get(u.id, True)}
                    for u in session.scalars(select(Participant).where(
                        Participant.event_id == event.id))]

    # consequences for the EXACT current grid — cached by a content fingerprint so
    # the 5s poll doesn't re-run CP-SAT when nothing relevant changed.
    fp = _eval_fingerprint(session, rnd)
    cache = request.app.state.eval_cache
    hit = cache.get(rnd.id)
    if hit and hit[0] == fp:
        result = hit[1]
    else:
        result = RD.evaluate_current(session, rnd.id)
        cache[rnd.id] = (fp, result)

    # present people who RATED but found nothing they'd attend (and run nothing) —
    # shown as not-covered, with their own reason rather than a capacity problem.
    prop_ids_all = [p.id for p in session.scalars(select(Proposal).where(
        Proposal.round_id == rnd.id))]
    rated_pids, interested_pids = set(), set()
    if prop_ids_all:
        for r in session.scalars(select(Rating).where(Rating.proposal_id.in_(prop_ids_all))):
            rated_pids.add(r.participant_id)
            if r.stars >= INTERESTED_STARS:
                interested_pids.add(r.participant_id)
    runner_pids = set()
    if prop_ids_all:
        runner_pids = {pr.participant_id for pr in session.scalars(
            select(ProposalRunner).where(ProposalRunner.proposal_id.in_(prop_ids_all)))}
    no_interest_ids = [u for u in rated_pids
                       if u not in interested_pids and u not in runner_pids and rp.get(u, True)]
    no_interest = sorted(names.get(u, u) for u in no_interest_ids)

    # coverage denominator includes the no-interest folks (they ARE not covered)
    denom = result.n_present + len(no_interest_ids)
    cov = {}
    for ts in timeslots:
        covered = result.n_present - len(result.left_out.get(ts.id, []))
        cov[ts.id] = round(100.0 * covered / denom, 1) if denom else 0.0
    left = {ts.id: [names.get(u, u) for u in result.left_out.get(ts.id, [])] for ts in timeslots}
    forced = {ts.id: [names.get(u, u) for u in result.forced_uncovered.get(ts.id, [])]
              for ts in timeslots}
    cellstats = {(pl.slot_id, pl.candidate_id): pl for pl in result.placements}

    unplaced = [x for x in pool if x["p"].id not in placed_ids]

    # diagnostics: why is the grid empty? (the #1 "solver doesn't work" cause)
    prop_ids = [p.id for p in session.scalars(select(Proposal).where(
        Proposal.round_id == rnd.id))]
    rated_count = 0
    if prop_ids:
        rated_count = session.scalar(select(func.count(func.distinct(Rating.participant_id)))
                                     .where(Rating.proposal_id.in_(prop_ids))) or 0

    n_eligible = sum(1 for x in pool if x["interested"] >= rnd.min_interest)
    slot_no_rooms = [ts.name for ts in timeslots if not ts_rooms[ts.id]]
    diag = {
        "has_timeslots": bool(timeslots),
        "slots_no_rooms": slot_no_rooms,
        "n_proposals": len(pool),
        "n_eligible": n_eligible,
        "n_rated": rated_count,
        "n_placed": len(placements),
    }

    ctx.update({
        "timeslots": timeslots, "ts_rooms": ts_rooms, "placements": placements,
        "placed_ids": placed_ids, "participants": participants,
        "coverage": cov, "left_out": left, "forced": forced, "cellstats": cellstats,
        "total_interest": result.total_interest, "unplaced": unplaced,
        "n_present": result.n_present, "no_interest": no_interest,
        "top_choice_pct": result.top_choice_pct,
        "published_drift": RD.published_drift(session, rnd.id) if rnd.status == "published" else False,
        "pin_conflicts": result.pin_conflicts, "diag": diag,
    })
    return ctx


def _respond(request: Request, session: Session, event: Event, round_id: int):
    """Return the console partial for htmx (no page reload), else a redirect."""
    rnd = owned_round(session, round_id, event.id)
    if request.headers.get("HX-Request"):
        ctx = _context(request, session, event, rnd)
        ctx["mode"] = "full"
        return render(request, "_console.html", **ctx)
    return RedirectResponse(f"/admin?round_id={round_id}", status_code=303)


def _respond_or_console(request: Request, session: Session, event: Event, round_id: int):
    """Like _respond but tolerates no round (event-level edits like rooms)."""
    if round_id:
        return _respond(request, session, event, round_id)
    rnd = _current_round(session, event.id, None)
    if request.headers.get("HX-Request"):
        ctx = _context(request, session, event, rnd)
        ctx["mode"] = "full"
        return render(request, "_console.html", **ctx)
    return RedirectResponse("/admin", status_code=303)


@router.get("")
def console(request: Request, round_id: int | None = None, mode: str = "full",
            token=Depends(require_admin), session: Session = Depends(get_session)):
    event = _event_of(session, token)
    rnd = _current_round(session, event.id, round_id)
    ctx = _context(request, session, event, rnd)
    ctx["mode"] = mode
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
    return _respond_or_console(request, session, event, round_id)


@router.post("/room-delete")
def room_delete(request: Request, room_id: int = Form(...), round_id: int = Form(0),
                token=Depends(require_admin), session: Session = Depends(get_session),
                _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_room(session, room_id, event.id)
    RD.delete_room(session, room_id)
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
def add_round(request: Request, name: str = Form(...), lambda_fairness: float = Form(3.0, ge=0, le=1000),
              min_interest: int = Form(1, ge=0), token=Depends(require_admin),
              session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    rnd = RD.create_round(session, event_id=event.id, name=name.strip(),
                          lambda_fairness=lambda_fairness, min_interest=min_interest)
    session.flush()
    return _respond(request, session, event, rnd.id)


@router.post("/timeslot")
def add_timeslot(request: Request, round_id: int = Form(...), name: str = Form(...),
                 duration_minutes: int = Form(45, ge=1, le=1440), room_ids: list[int] = Form([]),
                 token=Depends(require_admin), session: Session = Depends(get_session),
                 _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    pos = session.scalar(select(func.count(Timeslot.id)).where(
        Timeslot.round_id == round_id)) or 0
    RD.add_timeslot(session, round_id=round_id, name=name.strip(), position=pos,
                    duration_minutes=duration_minutes, room_ids=room_ids)
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


@router.post("/timeslot-rooms")
def timeslot_rooms(request: Request, round_id: int = Form(...),
                   timeslot_id: int = Form(...), room_ids: list[int] = Form([]),
                   token=Depends(require_admin),
                   session: Session = Depends(get_session),
                   _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    owned_timeslot(session, timeslot_id, event.id)
    for rid in room_ids:
        owned_room(session, rid, event.id)
    RD.set_timeslot_rooms(session, timeslot_id=timeslot_id, room_ids=room_ids)
    return _respond(request, session, event, round_id)


@router.post("/session-config")
def session_config(request: Request, round_id: int = Form(...), proposal_id: int = Form(...),
                   min_interest_override: str = Form(""), max_attendees: str = Form(""),
                   token=Depends(require_admin), session: Session = Depends(get_session),
                   _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    owned_proposal(session, proposal_id, event.id)
    mio = int(min_interest_override) if min_interest_override.strip().isdigit() else None
    mx = int(max_attendees) if max_attendees.strip().isdigit() else None
    P.set_config(session, proposal_id, min_interest_override=mio, max_attendees=mx)
    return _respond(request, session, event, round_id)


@router.post("/round-settings")
def round_settings(request: Request, round_id: int = Form(...),
                   lambda_fairness: float = Form(..., ge=0, le=1000),
                   min_interest: int = Form(..., ge=0),
                   token=Depends(require_admin), session: Session = Depends(get_session),
                   _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    rnd = owned_round(session, round_id, event.id)
    rnd.lambda_fairness = lambda_fairness
    rnd.min_interest = min_interest
    session.flush()
    return _respond(request, session, event, round_id)


# ---- solve & grid editing --------------------------------------------------

@router.post("/solve")
def solve(request: Request, round_id: int = Form(...), pin_started: int = Form(1),
          token=Depends(require_admin), session: Session = Depends(get_session),
          _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    rnd = owned_round(session, round_id, event.id)
    # protect sessions already in progress: pin them so regenerate keeps them put
    # (unless the admin explicitly chose to re-solve everything).
    if pin_started:
        RD.pin_started_placements(session, round_id)
    result = RD.solve_round(session, round_id, lam=rnd.lambda_fairness)
    RD.apply_result(session, round_id, result)
    return _respond(request, session, event, round_id)


@router.post("/pin")
def pin(request: Request, round_id: int = Form(...), placement_id: int = Form(...),
        pinned: int = Form(...), token=Depends(require_admin),
        session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_placement(session, placement_id, event.id)
    RD.set_pin(session, placement_id, bool(pinned))
    return _respond(request, session, event, round_id)


@router.post("/drop")
def drop(request: Request, round_id: int = Form(...), placement_id: int = Form(...),
         token=Depends(require_admin), session: Session = Depends(get_session),
         _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_placement(session, placement_id, event.id)
    RD.drop_placement(session, placement_id)
    return _respond(request, session, event, round_id)


@router.post("/move")
def move(request: Request, round_id: int = Form(...), placement_id: int = Form(...),
         timeslot_id: int = Form(...), room_id: int = Form(...),
         token=Depends(require_admin), session: Session = Depends(get_session),
         _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_placement(session, placement_id, event.id)
    owned_timeslot(session, timeslot_id, event.id)
    owned_room(session, room_id, event.id)
    room_in_timeslot(session, timeslot_id, room_id)
    moved = session.get(Placement, placement_id)
    _same_round(session, timeslot_id, moved.proposal_id)
    if session.scalar(select(Placement).where(Placement.timeslot_id == timeslot_id,
                                              Placement.room_id == room_id)):
        raise HTTPException(status_code=409, detail="cell already occupied")
    RD.move_placement(session, placement_id, timeslot_id=timeslot_id, room_id=room_id)
    return _respond(request, session, event, round_id)


@router.post("/force-place")
def force_place(request: Request, round_id: int = Form(...), timeslot_id: int = Form(...),
                room_id: int = Form(...), proposal_id: int = Form(...),
                token=Depends(require_admin), session: Session = Depends(get_session),
                _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_timeslot(session, timeslot_id, event.id)
    owned_proposal(session, proposal_id, event.id)
    owned_room(session, room_id, event.id)
    room_in_timeslot(session, timeslot_id, room_id)
    _same_round(session, timeslot_id, proposal_id)
    if session.scalar(select(Placement).where(Placement.timeslot_id == timeslot_id,
                                              Placement.room_id == room_id)):
        raise HTTPException(status_code=409, detail="cell already occupied")
    RD.force_place(session, timeslot_id=timeslot_id, room_id=room_id, proposal_id=proposal_id)
    return _respond(request, session, event, round_id)


def _same_round(session: Session, timeslot_id: int, proposal_id: int) -> None:
    """Guard: a placement must keep a proposal within its own round's grid."""
    ts = session.get(Timeslot, timeslot_id)
    pr = session.get(Proposal, proposal_id)
    if ts is None or pr is None or ts.round_id != pr.round_id:
        raise HTTPException(status_code=400, detail="proposal and timeslot are in different rounds")


@router.post("/event-descriptions")
def event_descriptions(request: Request, round_id: int = Form(...), enabled: int = Form(0),
                       token=Depends(require_admin), session: Session = Depends(get_session),
                       _: None = Depends(require_csrf)):
    """Toggle whether participants may attach a description when proposing."""
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    event.descriptions_enabled = bool(enabled)
    session.flush()
    return _respond(request, session, event, round_id)


# ---- proposal & participant management -------------------------------------

@router.post("/merge")
def merge(request: Request, round_id: int = Form(...), source_id: int = Form(...),
          target_id: int = Form(...), token=Depends(require_admin),
          session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    owned_proposal(session, source_id, event.id)
    owned_proposal(session, target_id, event.id)
    P.merge(session, source_id=source_id, target_id=target_id)
    return _respond(request, session, event, round_id)


@router.post("/retire")
def retire(request: Request, round_id: int = Form(...), proposal_id: int = Form(...),
           token=Depends(require_admin), session: Session = Depends(get_session),
           _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_proposal(session, proposal_id, event.id)
    P.retire(session, proposal_id)
    return _respond(request, session, event, round_id)


@router.post("/present")
def present(request: Request, round_id: int = Form(...), participant_id: int = Form(...),
            present: int = Form(...), token=Depends(require_admin),
            session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    owned_participant(session, participant_id, event.id)
    RD.set_present(session, round_id=round_id, participant_id=participant_id,
                   present=bool(present))
    return _respond(request, session, event, round_id)


# ---- publish / clocks / archive --------------------------------------------

@router.post("/publish")
def publish(request: Request, round_id: int = Form(...), token=Depends(require_admin),
            session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    RD.publish(session, round_id)
    return _respond(request, session, event, round_id)


@router.post("/unpublish")
def unpublish(request: Request, round_id: int = Form(...), token=Depends(require_admin),
              session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    RD.unpublish(session, round_id)
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


@router.post("/archive-round")
def archive_round(request: Request, round_id: int = Form(...), token=Depends(require_admin),
                  session: Session = Depends(get_session), _: None = Depends(require_csrf)):
    event = _event_of(session, token)
    owned_round(session, round_id, event.id)
    RD.archive(session, round_id)
    return RedirectResponse("/admin", status_code=303)


@router.get("/archive")
def archive_list(request: Request, token=Depends(require_admin),
                 session: Session = Depends(get_session)):
    event = _event_of(session, token)
    rounds = session.scalars(select(Round).where(Round.event_id == event.id)).all()
    rids = [r.id for r in rounds]
    snaps = []
    if rids:
        snaps = list(session.scalars(select(RoundSnapshot).where(
            RoundSnapshot.round_id.in_(rids)).order_by(RoundSnapshot.id.desc())))
    rnd_by_id = {r.id: r for r in rounds}
    return render(request, "archive.html", event=event, snaps=snaps, rnd_by_id=rnd_by_id)
