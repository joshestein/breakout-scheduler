from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Proposal, Rating, ProposalRunner, Round, Timeslot, Room
from app.types import Problem, Candidate, Slot, INTERESTED_STARS


def min_seated(p: Proposal, rnd: Round, biggest_room: int) -> int:
    """People besides the hosts who must be seated for `p` to run: the proposer's own
    number if set, else the round default. Lowered to the host's cap and to the
    biggest room, so a small workshop is never silently impossible. Never below 1."""
    n = p.min_interest_override if p.min_interest_override is not None else rnd.min_interest
    n = min(n, biggest_room)
    if p.max_attendees is not None:
        n = min(n, p.max_attendees)
    return max(1, n)


def build_problem(session: Session, round_id: int,
                  pins: frozenset[tuple[int, int, int]] = frozenset(),
                  force_include: frozenset[int] = frozenset()) -> Problem:
    """`force_include` proposal ids become candidates even if they would not be
    eligible. Used when scoring the actual grid so a placed-but-now-ineligible
    session isn't silently dropped from the stats."""
    rnd = session.get(Round, round_id)

    # proposals are scoped to this round (each round is self-contained)
    props = session.scalars(
        select(Proposal).where(Proposal.round_id == round_id)).all()
    prop_ids = [p.id for p in props]
    active = [p for p in props if p.status == "active"]
    active_ids = {p.id for p in active}

    stars: dict[tuple[int, int], int] = {}
    runners: dict[int, set[int]] = defaultdict(set)
    if prop_ids:
        for r in session.scalars(select(Rating).where(Rating.proposal_id.in_(prop_ids))):
            stars[(r.participant_id, r.proposal_id)] = r.stars
        for pr in session.scalars(select(ProposalRunner).where(
                ProposalRunner.proposal_id.in_(prop_ids))):
            runners[pr.proposal_id].add(pr.participant_id)

    # present = engaged: rated an active session >=3, or hosts one
    present: set[int] = {u for (u, cid), s in stars.items()
                         if cid in active_ids and s >= INTERESTED_STARS}
    for cid in active_ids:
        present |= runners[cid]

    # eligibility pre-filter: enough interested (>=3) people OTHER than the hosts to
    # meet the session's floor. The solver then enforces the floor on seats.
    def interested_count(cid: int) -> int:
        rated = {u for (u, k), s in stars.items() if k == cid and s >= INTERESTED_STARS}
        return len(rated - runners[cid])

    # every room of the event is available in every timeslot
    rooms = {r.id: r.capacity for r in session.scalars(
        select(Room).where(Room.event_id == rnd.event_id))}
    slots = [Slot(id=ts.id, position=ts.position, rooms=dict(rooms))
             for ts in session.scalars(select(Timeslot).where(
                 Timeslot.round_id == round_id).order_by(Timeslot.position))]

    biggest = max(rooms.values(), default=1)
    candidates = [
        Candidate(id=p.id, runners=frozenset(runners[p.id]), max_attendees=p.max_attendees,
                  min_seated=min_seated(p, rnd, biggest))
        for p in active
        if p.id in force_include or interested_count(p.id) >= min_seated(p, rnd, biggest)]
    cand_ids = {c.id for c in candidates}

    return Problem(slots=slots, candidates=candidates, present=frozenset(present),
                   stars={k: s for k, s in stars.items() if k[1] in cand_ids}, pins=pins)
