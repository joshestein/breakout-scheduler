from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.models import (Round, Timeslot, TimeslotRoom, Placement, Room, Proposal,
                        Participant, RoundParticipant, RoundSnapshot)
from app.types import Result
from app.solver_input import build_problem
from app.solver import build_and_solve


# ---- configuration helpers -------------------------------------------------

def create_round(session: Session, *, event_id: int, name: str,
                 lambda_fairness: float = 3.0, min_interest: int = 1) -> Round:
    r = Round(event_id=event_id, name=name, lambda_fairness=lambda_fairness,
              min_interest=min_interest, status="collecting")
    session.add(r); session.flush()
    return r


def add_timeslot(session: Session, *, round_id: int, name: str, position: int,
                 duration_minutes: int = 45, room_ids: list[int] | None = None) -> Timeslot:
    ts = Timeslot(round_id=round_id, name=name, position=position,
                  duration_minutes=duration_minutes)
    session.add(ts); session.flush()
    for rid in (room_ids or []):
        session.add(TimeslotRoom(timeslot_id=ts.id, room_id=rid))
    session.flush()
    return ts


def set_timeslot_rooms(session: Session, *, timeslot_id: int, room_ids: list[int]) -> None:
    """Replace the set of rooms available in a timeslot. Placements in removed
    rooms are dropped."""
    keep = set(room_ids)
    existing = {tr.room_id: tr for tr in session.scalars(
        select(TimeslotRoom).where(TimeslotRoom.timeslot_id == timeslot_id))}
    for room_id, tr in existing.items():
        if room_id not in keep:
            session.delete(tr)
    for room_id in keep - existing.keys():
        session.add(TimeslotRoom(timeslot_id=timeslot_id, room_id=room_id))
    for pl in session.scalars(select(Placement).where(Placement.timeslot_id == timeslot_id)):
        if pl.room_id not in keep:
            session.delete(pl)
    session.flush()


def schedule_markdown(session: Session, round_id: int) -> str:
    """Plain-text/Markdown of the placed schedule, paste-friendly for Slack/Discord:

        14:00 Breakouts
        - Library: AI safety
        - Den: Governance
    """
    rnd = session.get(Round, round_id)
    if rnd is None:
        return ""
    titles = {p.id: p.title for p in session.scalars(
        select(Proposal).where(Proposal.event_id == rnd.event_id))}
    rooms = {r.id: r.name for r in session.scalars(
        select(Room).where(Room.event_id == rnd.event_id))}
    blocks = []
    for ts in session.scalars(select(Timeslot).where(
            Timeslot.round_id == round_id).order_by(Timeslot.position)):
        cells = sorted(session.scalars(select(Placement).where(Placement.timeslot_id == ts.id)),
                       key=lambda pl: rooms.get(pl.room_id, ""))
        if not cells:
            continue
        lines = [f"{ts.name} Breakouts"]
        lines += [f"- {rooms.get(pl.room_id, '—')}: {titles.get(pl.proposal_id, '?')}"
                  for pl in cells]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def edit_room(session: Session, room_id: int, *, name: str, capacity: int) -> None:
    r = session.get(Room, room_id)
    r.name = name.strip() or r.name
    r.capacity = max(1, capacity)
    session.flush()


def delete_room(session: Session, room_id: int) -> None:
    """Remove a room and everything referencing it (placements, per-slot membership)."""
    session.execute(delete(Placement).where(Placement.room_id == room_id))
    session.execute(delete(TimeslotRoom).where(TimeslotRoom.room_id == room_id))
    session.execute(delete(Room).where(Room.id == room_id))
    session.flush()


def edit_timeslot(session: Session, timeslot_id: int, *, name: str,
                  duration_minutes: int) -> None:
    ts = session.get(Timeslot, timeslot_id)
    ts.name = name.strip() or ts.name
    ts.duration_minutes = max(1, duration_minutes)
    session.flush()


def delete_timeslot(session: Session, timeslot_id: int) -> None:
    session.execute(delete(Placement).where(Placement.timeslot_id == timeslot_id))
    session.execute(delete(TimeslotRoom).where(TimeslotRoom.timeslot_id == timeslot_id))
    session.execute(delete(Timeslot).where(Timeslot.id == timeslot_id))
    session.flush()


def set_present(session: Session, *, round_id: int, participant_id: int,
                present: bool) -> None:
    rp = session.scalar(select(RoundParticipant).where(
        RoundParticipant.round_id == round_id,
        RoundParticipant.participant_id == participant_id))
    if rp is None:
        session.add(RoundParticipant(round_id=round_id, participant_id=participant_id,
                                     present=present))
    else:
        rp.present = present
    session.flush()


# ---- solving ---------------------------------------------------------------

def _as_pins(placements: list[Placement]) -> frozenset[tuple[int, int, int]]:
    """Placements -> solver keys (proposal_id, timeslot_id, room_id)."""
    return frozenset((p.proposal_id, p.timeslot_id, p.room_id) for p in placements)


def _pins_from_placements(session: Session, round_id: int) -> frozenset[tuple[int, int, int]]:
    return _as_pins([p for p in _placements(session, round_id) if p.pinned])


def _all_placements_as_pins(session: Session, round_id: int) -> frozenset[tuple[int, int, int]]:
    return _as_pins(_placements(session, round_id))


def solve_round(session: Session, round_id: int, lam: float | None = None) -> Result:
    """Solve honoring pinned placements; does NOT write placements."""
    pins = _pins_from_placements(session, round_id)
    problem = build_problem(session, round_id, lam=lam, pins=pins)
    return build_and_solve(problem)


def evaluate_current(session: Session, round_id: int, lam: float | None = None) -> Result:
    """Stats for the exact current grid — the grid is FIXED to what's placed now,
    so the solver scores it as-is instead of inventing new placements."""
    fixed = _all_placements_as_pins(session, round_id)
    placed_ids = frozenset(pl.proposal_id for pl in _placements(session, round_id))
    problem = build_problem(session, round_id, lam=lam, force_include=placed_ids)
    problem = replace(problem, fixed=fixed)
    return build_and_solve(problem)


def apply_result(session: Session, round_id: int, result: Result, *,
                 keep_pinned: bool = True) -> None:
    """Write draft placements from a solver Result, preserving pinned ones."""
    existing = _placements(session, round_id)
    for p in existing:
        if not (keep_pinned and p.pinned):
            session.delete(p)
    session.flush()
    occupied = {(p.timeslot_id, p.room_id) for p in _placements(session, round_id)}
    for pl in result.placements:
        if (pl.slot_id, pl.room_id) in occupied:
            continue
        session.add(Placement(timeslot_id=pl.slot_id, room_id=pl.room_id,
                              proposal_id=pl.candidate_id, pinned=False))
        occupied.add((pl.slot_id, pl.room_id))
    session.flush()


def _placements(session: Session, round_id: int) -> list[Placement]:
    ts_ids = [t.id for t in session.scalars(
        select(Timeslot).where(Timeslot.round_id == round_id)).all()]
    if not ts_ids:
        return []
    return list(session.scalars(
        select(Placement).where(Placement.timeslot_id.in_(ts_ids))).all())


# ---- grid editing ----------------------------------------------------------

def set_pin(session: Session, placement_id: int, pinned: bool) -> None:
    session.get(Placement, placement_id).pinned = pinned
    session.flush()


def drop_placement(session: Session, placement_id: int) -> None:
    session.delete(session.get(Placement, placement_id))
    session.flush()


def move_placement(session: Session, placement_id: int, *, timeslot_id: int,
                   room_id: int) -> None:
    p = session.get(Placement, placement_id)
    p.timeslot_id = timeslot_id
    p.room_id = room_id
    session.flush()


def pin_started_placements(session: Session, round_id: int) -> int:
    """Pin every placement in a timeslot whose clock has already started, so a
    regenerate preserves sessions that are in progress. Returns how many were pinned."""
    started = [t.id for t in session.scalars(select(Timeslot).where(
        Timeslot.round_id == round_id, Timeslot.started_at.is_not(None)))]
    if not started:
        return 0
    n = 0
    for pl in session.scalars(select(Placement).where(Placement.timeslot_id.in_(started))):
        if not pl.pinned:
            pl.pinned = True
            n += 1
    session.flush()
    return n


def force_place(session: Session, *, timeslot_id: int, room_id: int,
                proposal_id: int) -> Placement:
    p = Placement(timeslot_id=timeslot_id, room_id=room_id, proposal_id=proposal_id,
                  pinned=True)
    session.add(p); session.flush()
    return p


# ---- publish / clocks ------------------------------------------------------

def _validate_rooms(session: Session, round_id: int) -> None:
    for p in _placements(session, round_id):
        ok = session.scalar(select(TimeslotRoom).where(
            TimeslotRoom.timeslot_id == p.timeslot_id,
            TimeslotRoom.room_id == p.room_id))
        if ok is None:
            ts = session.get(Timeslot, p.timeslot_id)
            room = session.get(Room, p.room_id)
            raise ValueError(
                f"placement of room {room.name!r} not available in slot {ts.name!r}")


def _snapshot_payload(session: Session, round_id: int) -> tuple[str, str]:
    result = evaluate_current(session, round_id)
    event_id = session.get(Round, round_id).event_id
    names = {u.id: u.display_name for u in session.scalars(
        select(Participant).where(Participant.event_id == event_id))}
    titles = {p.id: p.title for p in session.scalars(
        select(Proposal).where(Proposal.event_id == event_id))}
    rooms = {r.id: r.name for r in session.scalars(
        select(Room).where(Room.event_id == event_id))}
    slots = {t.id: t.name for t in session.scalars(
        select(Timeslot).where(Timeslot.round_id == round_id))}
    grid = [{
        "timeslot_id": pl.slot_id, "timeslot": slots.get(pl.slot_id),
        "room_id": pl.room_id, "room": rooms.get(pl.room_id),
        "proposal_id": pl.candidate_id, "title": titles.get(pl.candidate_id),
        "projected_attendance": pl.projected_attendance, "capacity": pl.capacity,
        "overflow": pl.overflow,
    } for pl in result.placements]
    stats = {
        "total_interest": result.total_interest,
        "coverage_pct": {slots.get(k, k): v for k, v in result.coverage_pct.items()},
        "left_out": {slots.get(k, k): [names.get(u, u) for u in v]
                     for k, v in result.left_out.items()},
    }
    return json.dumps(stats), json.dumps(grid)


def publish(session: Session, round_id: int) -> RoundSnapshot:
    """(Re)publish: freeze the current grid into a snapshot that becomes what
    attendees see. Keeps a single snapshot per round, so calling again refreshes
    it (used to push post-publish edits live)."""
    _validate_rooms(session, round_id)
    stats_json, grid_json = _snapshot_payload(session, round_id)
    session.execute(delete(RoundSnapshot).where(RoundSnapshot.round_id == round_id))
    snap = RoundSnapshot(round_id=round_id, stats_json=stats_json, grid_json=grid_json,
                         published_at=datetime.now(timezone.utc))
    session.add(snap)
    rnd = session.get(Round, round_id)
    rnd.status = "published"
    rnd.published_at = datetime.now(timezone.utc)
    session.flush()
    return snap


def published_snapshot(session: Session, round_id: int) -> RoundSnapshot | None:
    return session.scalar(select(RoundSnapshot).where(
        RoundSnapshot.round_id == round_id).order_by(RoundSnapshot.id.desc()))


def _current_lineup(session: Session, round_id: int) -> set:
    """The live (timeslot, room, proposal, title) tuples actually placed now."""
    event_id = session.get(Round, round_id).event_id
    titles = {p.id: p.title for p in session.scalars(
        select(Proposal).where(Proposal.event_id == event_id))}
    ts_ids = [t.id for t in session.scalars(
        select(Timeslot).where(Timeslot.round_id == round_id))]
    out = set()
    if ts_ids:
        for pl in session.scalars(select(Placement).where(Placement.timeslot_id.in_(ts_ids))):
            out.add((pl.timeslot_id, pl.room_id, pl.proposal_id, titles.get(pl.proposal_id)))
    return out


def published_drift(session: Session, round_id: int) -> bool:
    """True if the live lineup differs from what was published (so attendees would
    see something other than the published schedule once it's re-published)."""
    snap = published_snapshot(session, round_id)
    if snap is None:
        return False
    snap_set = {(e["timeslot_id"], e["room_id"], e["proposal_id"], e.get("title"))
                for e in json.loads(snap.grid_json)}
    return _current_lineup(session, round_id) != snap_set


def any_clock_started(session: Session, round_id: int) -> bool:
    return any(t.started_at is not None for t in session.scalars(
        select(Timeslot).where(Timeslot.round_id == round_id)).all())


def unpublish(session: Session, round_id: int) -> None:
    if any_clock_started(session, round_id):
        raise ValueError("cannot unpublish after a timeslot clock has started")
    session.execute(delete(RoundSnapshot).where(RoundSnapshot.round_id == round_id))
    session.get(Round, round_id).status = "collecting"
    session.flush()


def start_timeslot(session: Session, timeslot_id: int) -> None:
    session.get(Timeslot, timeslot_id).started_at = datetime.now(timezone.utc)
    session.flush()


def archive(session: Session, round_id: int) -> None:
    session.get(Round, round_id).status = "archived"
    session.flush()
