from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (Proposal, Rating, ProposalRunner, Round, RoundParticipant,
                        Timeslot, TimeslotRoom, Room)
from app.types import Problem, Candidate, Slot, INTERESTED_STARS
from app.merge_util import merge_root


def build_problem(session: Session, round_id: int, lam: float | None = None,
                  pins: frozenset[tuple[int, int, int]] = frozenset(),
                  force_include: frozenset[int] = frozenset()) -> Problem:
    """`force_include` proposal ids become candidates even if below the interest
    threshold — used when scoring the actual grid so a placed-but-now-ineligible
    session isn't silently dropped from the stats."""
    rnd = session.get(Round, round_id)
    lam = rnd.lambda_fairness if lam is None else lam

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

    # eligibility: >= min_interest people interested (>=3) counting present runners
    def interested_count(cid: int) -> int:
        rated = {u for (u, k), s in folded.items() if k == cid and s >= INTERESTED_STARS}
        return len(rated | effective_runners(cid))

    slots: list[Slot] = []
    for ts in session.scalars(
            select(Timeslot).where(Timeslot.round_id == round_id)
            .order_by(Timeslot.position)).all():
        rooms = {tr.room_id: session.get(Room, tr.room_id).capacity
                 for tr in session.scalars(
                     select(TimeslotRoom).where(TimeslotRoom.timeslot_id == ts.id)).all()}
        slots.append(Slot(id=ts.id, position=ts.position, rooms=rooms))

    def eff_min_interest(p: Proposal) -> int:
        return (p.min_interest_override if p.min_interest_override is not None
                else rnd.min_interest)

    candidates = [
        Candidate(id=p.id, runners=effective_runners(p.id), max_attendees=p.max_attendees)
        for p in active_roots
        if interested_count(p.id) >= eff_min_interest(p) or p.id in force_include]
    cand_ids = {c.id for c in candidates}

    stars = {(u, cid): s for (u, cid), s in folded.items() if cid in cand_ids}
    return Problem(slots=slots, candidates=candidates, present=present, stars=stars,
                   lam=lam, min_interest=rnd.min_interest, pins=pins)
