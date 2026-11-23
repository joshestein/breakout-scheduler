from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Proposal, ProposalRunner, Round
from app.merge_util import merge_root


def current_round_id(session: Session, event_id: int) -> int | None:
    """The round proposals are currently collected into: latest round still OPEN
    for input (draft/collecting). Never a published round — proposing into a
    published round would mutate an already-snapshotted pool."""
    r = session.scalar(select(Round).where(
        Round.event_id == event_id, Round.status.in_(("draft", "collecting"))
    ).order_by(Round.id.desc()))
    return r.id if r else None


def propose(session: Session, *, event_id: int, title: str, created_by: int,
            description: str | None = None, runner: bool = True,
            round_id: int | None = None, min_interest_override: int | None = None,
            max_attendees: int | None = None) -> Proposal:
    title = title.strip()
    if not title:
        raise ValueError("title required")
    if round_id is None:
        round_id = current_round_id(session, event_id)
    # normalise the optional host caps: positive ints only, and min <= max
    lo = min_interest_override if (min_interest_override or 0) > 0 else None
    hi = max_attendees if (max_attendees or 0) > 0 else None
    if lo is not None and hi is not None and lo > hi:
        lo = hi   # an impossible floor above the cap is clamped, not rejected
    p = Proposal(event_id=event_id, round_id=round_id, title=title,
                 description=(description or None), created_by=created_by, status="active",
                 min_interest_override=lo, max_attendees=hi)
    session.add(p)
    session.flush()
    # proposer is a runner by default (must be present); organizer-added topics
    # pass runner=False so nobody is forced to be present until someone opts in.
    if runner:
        set_runner(session, p.id, created_by, True)
        session.flush()
    return p


def set_config(session: Session, proposal_id: int, *,
               min_interest_override="keep", max_attendees="keep") -> None:
    """Update per-session scheduling config. Pass a value to change it; omit to keep.
    Both accept None to clear."""
    p = session.get(Proposal, proposal_id)
    if min_interest_override != "keep":
        p.min_interest_override = (min_interest_override
                                   if min_interest_override is not None else None)
    if max_attendees != "keep":
        p.max_attendees = max_attendees if (max_attendees or 0) > 0 else None
    # keep the same invariant as propose(): an impossible floor above the cap is clamped
    if (p.min_interest_override is not None and p.max_attendees is not None
            and p.min_interest_override > p.max_attendees):
        p.min_interest_override = p.max_attendees
    session.flush()


def edit_title(session: Session, proposal_id: int, title: str) -> None:
    t = title.strip()
    if not t:
        raise ValueError("title required")
    session.get(Proposal, proposal_id).title = t[:300]
    session.flush()


def retire(session: Session, proposal_id: int) -> None:
    p = session.get(Proposal, proposal_id)
    p.status = "retired"
    session.flush()


def reactivate(session: Session, proposal_id: int) -> None:
    p = session.get(Proposal, proposal_id)
    p.status = "active"
    session.flush()


def set_runner(session: Session, proposal_id: int, participant_id: int, on: bool) -> None:
    existing = session.scalar(
        select(ProposalRunner).where(ProposalRunner.proposal_id == proposal_id,
                                     ProposalRunner.participant_id == participant_id))
    if on and existing is None:
        session.add(ProposalRunner(proposal_id=proposal_id, participant_id=participant_id))
    elif not on and existing is not None:
        session.delete(existing)
    session.flush()


def _parent_map(session: Session, event_id: int) -> dict[int, int | None]:
    return {p.id: p.merged_into for p in session.scalars(
        select(Proposal).where(Proposal.event_id == event_id)).all()}


def merge(session: Session, *, source_id: int, target_id: int) -> None:
    """Merge `source` into `target` (non-destructive: only sets merged_into/status).

    Ratings and runners are NEVER mutated; folding happens at read time in
    solver_input. Re-points to the target's root and rejects cycles.
    """
    if source_id == target_id:
        raise ValueError("cannot merge a proposal into itself")
    src = session.get(Proposal, source_id)
    target = session.get(Proposal, target_id)
    # rounds are self-contained: merging across rounds would delete a session from
    # one round and drop its votes (they don't fold into another round's pool).
    if target is None or src.round_id != target.round_id:
        raise ValueError("can only merge proposals within the same round")
    parent = _parent_map(session, src.event_id)
    target_root = merge_root(target_id, parent)
    if target_root == source_id:
        raise ValueError("merge would create a cycle")
    src.merged_into = target_root
    src.status = "retired"
    session.flush()


def unmerge(session: Session, source_id: int) -> None:
    src = session.get(Proposal, source_id)
    src.merged_into = None
    src.status = "active"
    session.flush()
