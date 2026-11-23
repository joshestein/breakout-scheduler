from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (Proposal, Rating, ProposalRunner, Round, RoundParticipant,
                        Timeslot, TimeslotRoom, Room)
from app.types import Problem, Candidate, Slot, INTERESTED_STARS
from app.merge_util import merge_root


@dataclass(frozen=True)
class Eligibility:
    """Whether a session can be a solver candidate, and why not. The admin console
    reads this too, so its counts and "won't run" badges match what Generate does."""
    interested: int   # present people besides the hosts who rated it 3+ stars
    floor: int        # people besides the hosts who must be seated for it to run
    hosts_away: bool  # it has hosts, and all of them are marked away

    @property
    def eligible(self) -> bool:
        return self.interested >= self.floor and not self.hosts_away


@dataclass(frozen=True)
class _RoundData:
    rnd: Round
    active_roots: list[Proposal]
    folded: dict[tuple[int, int], int]   # (participant, merge root) -> max stars
    runners: dict[int, set[int]]          # merge root -> hosts, present or away
    present_flag: dict[int, bool]         # participant -> present (missing = present)
    slots: list[Slot]

    def is_present(self, u: int) -> bool:
        return self.present_flag.get(u, True)

    def effective_runners(self, cid: int) -> frozenset[int]:
        """Hosts who are present. An away host's presence isn't required."""
        return frozenset(u for u in self.runners.get(cid, ()) if self.is_present(u))


def _load(session: Session, round_id: int) -> _RoundData:
    rnd = session.get(Round, round_id)
    if rnd is None:
        raise ValueError(f"no round {round_id}")

    # proposals are scoped to this round (each round is self-contained)
    props = session.scalars(
        select(Proposal).where(Proposal.round_id == round_id)).all()
    parent = {p.id: p.merged_into for p in props}
    prop_ids = list(parent)
    active_roots = [p for p in props if p.status == "active" and p.merged_into is None]

    # fold ratings to their merge-root (max stars wins); non-destructive view.
    # Scope the query to THIS round's proposals, not every rating in the DB.
    folded: dict[tuple[int, int], int] = {}
    if prop_ids:
        for r in session.scalars(select(Rating).where(Rating.proposal_id.in_(prop_ids))):
            root = merge_root(r.proposal_id, parent)
            key = (r.participant_id, root)
            folded[key] = max(folded.get(key, 0), r.stars)

    # fold runners to root (also scoped to this round's proposals)
    runners: dict[int, set[int]] = defaultdict(set)
    if prop_ids:
        for pr in session.scalars(select(ProposalRunner).where(
                ProposalRunner.proposal_id.in_(prop_ids))):
            runners[merge_root(pr.proposal_id, parent)].add(pr.participant_id)

    present_flag = {x.participant_id: x.present for x in session.scalars(
        select(RoundParticipant).where(RoundParticipant.round_id == round_id)).all()}

    slots: list[Slot] = []
    for ts in session.scalars(
            select(Timeslot).where(Timeslot.round_id == round_id)
            .order_by(Timeslot.position)).all():
        rooms = {tr.room_id: session.get(Room, tr.room_id).capacity
                 for tr in session.scalars(
                     select(TimeslotRoom).where(TimeslotRoom.timeslot_id == ts.id)).all()}
        slots.append(Slot(id=ts.id, position=ts.position, rooms=rooms))

    return _RoundData(rnd=rnd, active_roots=active_roots, folded=folded,
                      runners=dict(runners), present_flag=present_flag, slots=slots)


def _eligibility(d: _RoundData) -> dict[int, Eligibility]:
    # largest room any slot offers: a floor above it could never be met
    biggest_room = max((cap for s in d.slots for cap in s.rooms.values()), default=1)

    # present people who rated each session 3+ stars (merged children folded in)
    raters: dict[int, set[int]] = defaultdict(set)
    for (u, cid), stars in d.folded.items():
        if stars >= INTERESTED_STARS and d.is_present(u):
            raters[cid].add(u)

    out: dict[int, Eligibility] = {}
    for p in d.active_roots:
        # the proposer's number, else the round default. Clamped to the host's cap and
        # to the biggest room, so a small workshop is never silently impossible.
        n = (p.min_interest_override if p.min_interest_override is not None
             else d.rnd.min_interest)
        n = min(n, biggest_room)
        if p.max_attendees is not None:
            n = min(n, p.max_attendees)
        hosts = d.runners.get(p.id, set())
        out[p.id] = Eligibility(
            interested=len(raters[p.id] - hosts),
            floor=max(1, n),
            # organizer-added sessions with no host at all can still run
            hosts_away=bool(hosts) and not d.effective_runners(p.id))
    return out


def eligibility(session: Session, round_id: int) -> dict[int, Eligibility]:
    """Eligibility of each active, unmerged proposal in the round, by proposal id."""
    return _eligibility(_load(session, round_id))


def build_problem(session: Session, round_id: int,
                  pins: frozenset[tuple[int, int, int]] = frozenset(),
                  force_include: frozenset[int] = frozenset()) -> Problem:
    """`force_include` proposal ids become candidates even if they would not be
    eligible. Used when scoring the actual grid so a placed-but-now-ineligible
    session isn't silently dropped from the stats."""
    d = _load(session, round_id)
    elig = _eligibility(d)

    # presence: engaged (rated >=3 a candidate OR runner of one) AND present-flag
    root_ids = {p.id for p in d.active_roots}
    engaged: set[int] = {u for (u, cid), s in d.folded.items()
                         if cid in root_ids and s >= INTERESTED_STARS}
    for cid in root_ids:
        engaged |= d.runners.get(cid, set())
    present = frozenset(u for u in engaged if d.is_present(u))

    candidates = [
        Candidate(id=p.id, runners=d.effective_runners(p.id),
                  max_attendees=p.max_attendees, min_seated=elig[p.id].floor)
        for p in d.active_roots
        if p.id in force_include or elig[p.id].eligible]
    cand_ids = {c.id for c in candidates}

    stars = {(u, cid): s for (u, cid), s in d.folded.items() if cid in cand_ids}
    return Problem(slots=d.slots, candidates=candidates, present=present, stars=stars,
                   pins=pins)
