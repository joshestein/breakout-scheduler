from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.models import (Assignment, Round, Timeslot, Placement, Room, Proposal,
                        ProposalRunner, Participant)
from app.types import Result
from app.solver_input import build_problem
from app.solver import build_and_solve


# ---- configuration helpers -------------------------------------------------

def create_round(session: Session, *, event_id: int, name: str,
                 min_interest: int = 2) -> Round:
    r = Round(event_id=event_id, name=name, min_interest=min_interest, status="collecting")
    session.add(r); session.flush()
    return r


def input_round(session: Session, event_id: int) -> Round | None:
    """The oldest round still collecting proposals and ratings. Rounds open one at a
    time in creation order: a later round opens when the one before it is published.
    Never a published round: a new proposal would not be on its grid."""
    return session.scalar(select(Round).where(
        Round.event_id == event_id, Round.status == "collecting").order_by(Round.id.asc()))


def published_round(session: Session, event_id: int) -> Round | None:
    """The latest published round: the one attendees see."""
    return session.scalar(select(Round).where(
        Round.event_id == event_id, Round.status == "published").order_by(Round.id.desc()))


def add_timeslot(session: Session, *, round_id: int, name: str, position: int,
                 duration_minutes: int = 45) -> Timeslot:
    ts = Timeslot(round_id=round_id, name=name, position=position,
                  duration_minutes=duration_minutes)
    session.add(ts); session.flush()
    return ts



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
        lines += [f"- {rooms.get(pl.room_id, '?')}: {titles.get(pl.proposal_id, '?')}"
                  for pl in cells]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def edit_room(session: Session, room_id: int, *, name: str, capacity: int) -> None:
    r = session.get(Room, room_id)
    r.name = name.strip() or r.name
    r.capacity = max(1, capacity)
    session.flush()


def delete_room(session: Session, room_id: int) -> None:
    """Remove a room and its placements."""
    session.execute(delete(Placement).where(Placement.room_id == room_id))
    session.execute(delete(Room).where(Room.id == room_id))
    session.flush()


def edit_timeslot(session: Session, timeslot_id: int, *, name: str,
                  duration_minutes: int) -> None:
    ts = session.get(Timeslot, timeslot_id)
    ts.name = name.strip() or ts.name
    ts.duration_minutes = max(1, duration_minutes)
    session.flush()


def delete_timeslot(session: Session, timeslot_id: int) -> None:
    session.execute(delete(Assignment).where(Assignment.timeslot_id == timeslot_id))
    session.execute(delete(Placement).where(Placement.timeslot_id == timeslot_id))
    session.execute(delete(Timeslot).where(Timeslot.id == timeslot_id))
    session.flush()



# ---- solving ---------------------------------------------------------------

def _as_pins(placements: list[Placement]) -> frozenset[tuple[int, int, int]]:
    """Placements -> solver keys (proposal_id, timeslot_id, room_id)."""
    return frozenset((p.proposal_id, p.timeslot_id, p.room_id) for p in placements)


def solve_round(session: Session, round_id: int) -> Result:
    """Solve honoring pinned placements; does NOT write placements. Pinned sessions
    are kept even if they no longer meet their floor (the admin chose them)."""
    pins = _as_pins([p for p in _placements(session, round_id) if p.pinned])
    pinned_ids = frozenset(cid for (cid, _, _) in pins)
    problem = build_problem(session, round_id, pins=pins, force_include=pinned_ids)
    return build_and_solve(problem)


def evaluate_current(session: Session, round_id: int) -> Result:
    """Stats for the exact current grid — the grid is FIXED to what's placed now,
    so the solver scores it as-is instead of inventing new placements."""
    placements = _placements(session, round_id)
    fixed = _as_pins(placements)
    placed_ids = frozenset(pl.proposal_id for pl in placements)
    problem = build_problem(session, round_id, force_include=placed_ids)
    problem = replace(problem, fixed=fixed)
    return build_and_solve(problem)


def apply_result(session: Session, round_id: int, result: Result) -> None:
    """Write draft placements from a solver Result, preserving pinned ones.

    Raises ValueError on a non-solution so an infeasible solve never wipes the grid."""
    if result.status == "UNKNOWN":
        raise ValueError("The solver ran out of time before it found a schedule. "
                         "Grid not changed. Try again.")
    if result.status not in ("OPTIMAL", "FEASIBLE"):
        raise ValueError("No schedule fits the pins and rules. Grid not changed.")
    for p in _placements(session, round_id):
        if not p.pinned:
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


def save_assignments(session: Session, round_id: int, result: Result) -> None:
    """Replace the round's saved seating with `result`'s. A grid that breaks a rule
    has no seating, so it clears the round's assignments."""
    ts_ids = [t.id for t in session.scalars(
        select(Timeslot).where(Timeslot.round_id == round_id)).all()]
    if not ts_ids:
        return
    session.execute(delete(Assignment).where(Assignment.timeslot_id.in_(ts_ids)))
    for (u, slot_id), cid in result.assignments.items():
        session.add(Assignment(timeslot_id=slot_id, participant_id=u, proposal_id=cid))
    session.flush()


def assignments_of(session: Session, round_id: int, participant_id: int) -> dict[int, int]:
    """timeslot_id -> proposal_id where this person is seated in the round."""
    return {a.timeslot_id: a.proposal_id for a in session.scalars(
        select(Assignment).join(Timeslot, Timeslot.id == Assignment.timeslot_id).where(
            Timeslot.round_id == round_id, Assignment.participant_id == participant_id))}


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


def remove_session(session: Session, proposal_id: int) -> None:
    """Retire a session and take it off the grid. Generate never places a retired
    session, so the removal sticks until it is restored."""
    session.execute(delete(Placement).where(Placement.proposal_id == proposal_id))
    session.get(Proposal, proposal_id).status = "retired"
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


def place(session: Session, *, timeslot_id: int, room_id: int,
          proposal_id: int) -> Placement:
    """Hand placement (pinned). The cell's old session goes back to the pool, and
    the session moves if it was already placed elsewhere in the round.

    Raises ValueError (and changes nothing) if it would put a host in two
    sessions in one timeslot."""
    ts = session.get(Timeslot, timeslot_id)
    placements = _placements(session, ts.round_id)
    target = next((pl for pl in placements
                   if (pl.timeslot_id, pl.room_id) == (timeslot_id, room_id)), None)
    current = next((pl for pl in placements if pl.proposal_id == proposal_id), None)
    if target is not None and target is current:
        target.pinned = True
        session.flush()
        return target

    def hosts(pid: int) -> set[int]:
        return set(session.scalars(select(ProposalRunner.participant_id).where(
            ProposalRunner.proposal_id == pid)))
    mine = hosts(proposal_id)
    for pl in placements:
        if pl.timeslot_id != timeslot_id or pl is target or pl is current:
            continue
        clash = mine & hosts(pl.proposal_id)
        if clash:
            who = ", ".join(sorted(session.get(Participant, u).display_name for u in clash))
            title = session.get(Proposal, pl.proposal_id).title
            raise ValueError(f"{who} already hosts “{title}” in {ts.name}.")

    for pl in (target, current):
        if pl is not None:
            session.delete(pl)
    session.flush()
    p = Placement(timeslot_id=timeslot_id, room_id=room_id, proposal_id=proposal_id,
                  pinned=True)
    session.add(p); session.flush()
    return p


# ---- publish / clocks ------------------------------------------------------

def publish(session: Session, round_id: int) -> None:
    """Show the round to attendees. They see the live grid, so later edits are
    visible at once."""
    rnd = session.get(Round, round_id)
    rnd.status = "published"
    rnd.published_at = datetime.now(timezone.utc)
    session.flush()


def any_clock_started(session: Session, round_id: int) -> bool:
    return any(t.started_at is not None for t in session.scalars(
        select(Timeslot).where(Timeslot.round_id == round_id)).all())


def unpublish(session: Session, round_id: int) -> None:
    if any_clock_started(session, round_id):
        raise ValueError("cannot unpublish after a timeslot clock has started")
    session.get(Round, round_id).status = "collecting"
    session.flush()


def start_timeslot(session: Session, timeslot_id: int) -> None:
    session.get(Timeslot, timeslot_id).started_at = datetime.now(timezone.utc)
    session.flush()
