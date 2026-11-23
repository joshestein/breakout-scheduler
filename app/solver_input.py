from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (Proposal, Rating, ProposalRunner, Round, RoundParticipant,
                        Timeslot, TimeslotRoom, Room)
from app.types import Problem, Candidate, Slot, INTERESTED_STARS
from app.merge_util import merge_root


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
    parent = {p.id: p.merged_into for p in props}
    prop_ids = list(parent)
    active_roots = [p for p in props if p.status == "active" and p.merged_into is None]
    candidate_ids = {p.id for p in active_roots}

    # fold ratings to their merge-root (max stars wins); non-destructive view.
    # Scope the query to THIS round's proposals — not every rating in the DB.
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

    # presence: engaged (rated >=3 a candidate OR runner of one) AND present-flag
    rp = {x.participant_id: x.present for x in session.scalars(
        select(RoundParticipant).where(RoundParticipant.round_id == round_id)).all()}
    engaged: set[int] = {u for (u, cid), s in folded.items()
                         if cid in candidate_ids and s >= INTERESTED_STARS}
    for cid in candidate_ids:
        engaged |= runners[cid]
    present = frozenset(u for u in engaged if rp.get(u, True))

    # drop absent runners from candidate runner-sets (their presence isn't required)
    def effective_runners(cid: int) -> frozenset[int]:
        return frozenset(u for u in runners[cid] if rp.get(u, True))

    # eligibility pre-filter: enough interested (>=3) people OTHER than the hosts to
    # meet the session's floor. The solver then enforces the floor on seats.
    def interested_count(cid: int) -> int:
        rated = {u for (u, k), s in folded.items() if k == cid and s >= INTERESTED_STARS}
        return len(rated - runners[cid])

    # a session whose hosts are all marked away can't run (organizer-added sessions
    # with no host at all are fine)
    def hosts_away(cid: int) -> bool:
        return bool(runners[cid]) and not effective_runners(cid)

    slots: list[Slot] = []
    for ts in session.scalars(
            select(Timeslot).where(Timeslot.round_id == round_id)
            .order_by(Timeslot.position)).all():
        rooms = {tr.room_id: session.get(Room, tr.room_id).capacity
                 for tr in session.scalars(
                     select(TimeslotRoom).where(TimeslotRoom.timeslot_id == ts.id)).all()}
        slots.append(Slot(id=ts.id, position=ts.position, rooms=rooms))

    # largest room any slot offers: a floor above it could never be met
    biggest_room = max((cap for s in slots for cap in s.rooms.values()), default=1)

    def min_seated(p: Proposal) -> int:
        """Non-hosts who must be seated: the proposer's number, else the round default.
        Clamped to the host's cap and to the biggest room, so a small workshop is never
        silently impossible. Never below 1."""
        n = (p.min_interest_override if p.min_interest_override is not None
             else rnd.min_interest)
        n = min(n, biggest_room)
        if p.max_attendees is not None:
            n = min(n, p.max_attendees)
        return max(1, n)

    candidates = [
        Candidate(id=p.id, runners=effective_runners(p.id), max_attendees=p.max_attendees,
                  min_seated=min_seated(p))
        for p in active_roots
        if p.id in force_include
        or (interested_count(p.id) >= min_seated(p) and not hosts_away(p.id))]
    cand_ids = {c.id for c in candidates}

    stars = {(u, cid): s for (u, cid), s in folded.items() if cid in cand_ids}
    return Problem(slots=slots, candidates=candidates, present=present, stars=stars,
                   pins=pins)
