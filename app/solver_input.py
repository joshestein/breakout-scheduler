from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Proposal, Rating, ProposalRunner, Round, Timeslot, Room
from app.types import Problem, Candidate, Slot, INTERESTED_STARS


@dataclass(frozen=True)
class Eligibility:
    """Whether a session can be a solver candidate. The admin console reads this too,
    so its counts and reasons match what Generate does."""
    interested: int   # people besides the hosts who rated it 3+ stars
    floor: int        # people besides the hosts who must be seated for it to run
    hosted: bool      # at least one person runs it; a session with no host never runs

    @property
    def eligible(self) -> bool:
        return self.hosted and self.interested >= self.floor


@dataclass(frozen=True)
class _RoundData:
    rnd: Round
    props: list[Proposal]
    stars: dict[tuple[int, int], int]   # (participant, proposal) -> stars
    runners: dict[int, set[int]]         # proposal -> hosts
    slots: list[Slot]


def _load(session: Session, round_id: int) -> _RoundData:
    rnd = session.get(Round, round_id)
    if rnd is None:
        raise ValueError(f"no round {round_id}")

    # proposals are scoped to this round (each round is self-contained)
    props = list(session.scalars(
        select(Proposal).where(Proposal.round_id == round_id).order_by(Proposal.id)))
    prop_ids = [p.id for p in props]

    stars: dict[tuple[int, int], int] = {}
    runners: dict[int, set[int]] = defaultdict(set)
    if prop_ids:
        for r in session.scalars(select(Rating).where(Rating.proposal_id.in_(prop_ids))):
            stars[(r.participant_id, r.proposal_id)] = r.stars
        for pr in session.scalars(select(ProposalRunner).where(
                ProposalRunner.proposal_id.in_(prop_ids))):
            runners[pr.proposal_id].add(pr.participant_id)

    # every room of the event is available in every timeslot
    rooms = {r.id: r.capacity for r in session.scalars(
        select(Room).where(Room.event_id == rnd.event_id))}
    slots = [Slot(id=ts.id, position=ts.position, rooms=dict(rooms))
             for ts in session.scalars(select(Timeslot).where(
                 Timeslot.round_id == round_id).order_by(Timeslot.position))]
    return _RoundData(rnd=rnd, props=props, stars=stars, runners=dict(runners), slots=slots)


def _eligibility(d: _RoundData) -> dict[int, Eligibility]:
    # largest room any slot offers: a floor above it could never be met
    biggest_room = max((cap for s in d.slots for cap in s.rooms.values()), default=1)

    raters: dict[int, set[int]] = defaultdict(set)
    for (u, cid), s in d.stars.items():
        if s >= INTERESTED_STARS:
            raters[cid].add(u)

    out: dict[int, Eligibility] = {}
    for p in d.props:
        # the proposer's number, else the round default. Lowered to the host's cap and
        # to the biggest room, so a small workshop is never silently impossible.
        n = (p.min_interest_override if p.min_interest_override is not None
             else d.rnd.min_interest)
        n = min(n, biggest_room)
        if p.max_attendees is not None:
            n = min(n, p.max_attendees)
        hosts = d.runners.get(p.id, set())
        out[p.id] = Eligibility(interested=len(raters[p.id] - hosts),
                                floor=max(1, n), hosted=bool(hosts))
    return out


def eligibility(session: Session, round_id: int) -> dict[int, Eligibility]:
    """Eligibility of each proposal in the round, by proposal id."""
    return _eligibility(_load(session, round_id))


def build_problem(session: Session, round_id: int,
                  pins: frozenset[tuple[int, int, int]] = frozenset(),
                  force_include: frozenset[int] = frozenset()) -> Problem:
    """`force_include` proposal ids become candidates even if they would not be
    eligible (no host, or too few interested). Used for pinned sessions and when
    scoring the actual grid, so a hand edit is flagged, never silently dropped."""
    d = _load(session, round_id)
    elig = _eligibility(d)
    active = [p for p in d.props if p.status == "active"]
    active_ids = {p.id for p in active}

    # present = engaged: rated an active session >=3, or hosts one
    present: set[int] = {u for (u, cid), s in d.stars.items()
                         if cid in active_ids and s >= INTERESTED_STARS}
    for cid in active_ids:
        present |= d.runners.get(cid, set())

    candidates = [
        Candidate(id=p.id, runners=frozenset(d.runners.get(p.id, set())),
                  max_attendees=p.max_attendees, min_seated=elig[p.id].floor)
        for p in active
        if p.id in force_include or elig[p.id].eligible]
    cand_ids = {c.id for c in candidates}

    return Problem(slots=d.slots, candidates=candidates, present=frozenset(present),
                   stars={k: s for k, s in d.stars.items() if k[1] in cand_ids}, pins=pins)
