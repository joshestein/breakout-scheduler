"""Ownership resolvers: load an object by id and assert it belongs to the
caller's event, else 404. Prevents cross-event (IDOR) manipulation when one
deployment hosts multiple events."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Round, Proposal, Timeslot, Placement, Participant, Room


def _deny():
    raise HTTPException(status_code=404, detail="not found in this event")


def owned_round(session: Session, round_id: int, event_id: int) -> Round:
    r = session.get(Round, round_id)
    if r is None or r.event_id != event_id:
        _deny()
    return r


def owned_proposal(session: Session, proposal_id: int, event_id: int) -> Proposal:
    p = session.get(Proposal, proposal_id)
    if p is None or p.event_id != event_id:
        _deny()
    return p


def owned_timeslot(session: Session, timeslot_id: int, event_id: int) -> Timeslot:
    ts = session.get(Timeslot, timeslot_id)
    if ts is None:
        _deny()
    owned_round(session, ts.round_id, event_id)
    return ts


def owned_placement(session: Session, placement_id: int, event_id: int) -> Placement:
    pl = session.get(Placement, placement_id)
    if pl is None:
        _deny()
    owned_timeslot(session, pl.timeslot_id, event_id)
    return pl


def owned_participant(session: Session, participant_id: int, event_id: int) -> Participant:
    u = session.get(Participant, participant_id)
    if u is None or u.event_id != event_id:
        _deny()
    return u


def owned_room(session: Session, room_id: int, event_id: int) -> Room:
    r = session.get(Room, room_id)
    if r is None or r.event_id != event_id:
        _deny()
    return r

