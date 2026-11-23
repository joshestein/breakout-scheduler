import pytest

from app.models import (Event, Room, Participant, Proposal, ProposalRunner, Rating,
                        Round, RoundParticipant, Timeslot, TimeslotRoom)
from app.solver_input import build_problem
from app.merge_util import merge_root


def _base_event(s):
    e = Event(name="R", participant_code="p", admin_code="a"); s.add(e); s.flush()
    room = Room(event_id=e.id, name="Library", capacity=10); s.add(room); s.flush()
    rnd = Round(event_id=e.id, name="Round 1", lambda_fairness=3.0, min_interest=1)
    s.add(rnd); s.flush()
    ts = Timeslot(round_id=rnd.id, name="Slot A", position=0); s.add(ts); s.flush()
    s.add(TimeslotRoom(timeslot_id=ts.id, room_id=room.id)); s.flush()
    return e, room, rnd, ts


def _participant(s, e, name):
    u = Participant(event_id=e.id, display_name=name); s.add(u); s.flush()
    return u


def test_only_active_unmerged_become_candidates(session):
    s = session
    e, room, rnd, ts = _base_event(s)
    u = _participant(s, e, "Sam")
    active = Proposal(event_id=e.id, round_id=rnd.id, title="Active", created_by=u.id, status="active")
    retired = Proposal(event_id=e.id, round_id=rnd.id, title="Retired", created_by=u.id, status="retired")
    s.add_all([active, retired]); s.flush()
    s.add(Rating(participant_id=u.id, proposal_id=active.id, stars=5))
    s.add(Rating(participant_id=u.id, proposal_id=retired.id, stars=5)); s.flush()
    p = build_problem(s, rnd.id)
    assert {c.id for c in p.candidates} == {active.id}


def test_merge_folds_max_stars_and_unions_runners(session):
    s = session
    e, room, rnd, ts = _base_event(s)
    u1 = _participant(s, e, "U1"); u2 = _participant(s, e, "U2")
    a = Proposal(event_id=e.id, round_id=rnd.id, title="A", created_by=u1.id); s.add(a); s.flush()
    b = Proposal(event_id=e.id, round_id=rnd.id, title="B", created_by=u2.id); s.add(b); s.flush()
    s.add(Rating(participant_id=u1.id, proposal_id=a.id, stars=3))
    s.add(Rating(participant_id=u1.id, proposal_id=b.id, stars=5))
    s.add(ProposalRunner(proposal_id=b.id, participant_id=u2.id)); s.flush()
    # merge b into a (non-destructive)
    b.merged_into = a.id; b.status = "retired"; s.flush()
    p = build_problem(s, rnd.id)
    assert {c.id for c in p.candidates} == {a.id}
    assert p.stars[(u1.id, a.id)] == 5            # folded max(3,5)
    assert u2.id in next(c for c in p.candidates if c.id == a.id).runners


def test_presence_requires_engaged_and_present_flag(session):
    s = session
    e, room, rnd, ts = _base_event(s)
    engaged = _participant(s, e, "Eng")
    absent = _participant(s, e, "Abs")
    lurker = _participant(s, e, "Lurk")
    a = Proposal(event_id=e.id, round_id=rnd.id, title="A", created_by=engaged.id); s.add(a); s.flush()
    s.add(Rating(participant_id=engaged.id, proposal_id=a.id, stars=5))
    s.add(Rating(participant_id=absent.id, proposal_id=a.id, stars=5))
    s.add(Rating(participant_id=lurker.id, proposal_id=a.id, stars=2))  # not engaged
    s.add(RoundParticipant(round_id=rnd.id, participant_id=absent.id, present=False))
    s.flush()
    p = build_problem(s, rnd.id)
    assert engaged.id in p.present
    assert absent.id not in p.present       # flagged absent
    assert lurker.id not in p.present        # only 2 stars => not engaged


def test_min_interest_excludes_runner_only_session(session):
    s = session
    e, room, rnd, ts = _base_event(s)
    rnd.min_interest = 2
    u = _participant(s, e, "Solo")
    a = Proposal(event_id=e.id, round_id=rnd.id, title="Lonely", created_by=u.id); s.add(a); s.flush()
    s.add(ProposalRunner(proposal_id=a.id, participant_id=u.id)); s.flush()
    p = build_problem(s, rnd.id)
    assert all(c.id != a.id for c in p.candidates)   # only 1 interested (the runner)


def test_merge_cycle_rejected():
    with pytest.raises(ValueError):
        merge_root(1, {1: 2, 2: 1})


def test_floor_is_clamped_to_host_cap(session):
    # round minimum 2, but the host caps the workshop at 1 guest: it must still be
    # able to run (floor lowered to 1), not silently never run
    s = session
    e, room, rnd, ts = _base_event(s)
    rnd.min_interest = 2
    host = _participant(s, e, "Host"); guest = _participant(s, e, "Guest")
    a = Proposal(event_id=e.id, round_id=rnd.id, title="Tiny", created_by=host.id,
                 max_attendees=1); s.add(a); s.flush()
    s.add(ProposalRunner(proposal_id=a.id, participant_id=host.id))
    s.add(Rating(participant_id=guest.id, proposal_id=a.id, stars=5)); s.flush()
    p = build_problem(s, rnd.id)
    assert [c.min_seated for c in p.candidates if c.id == a.id] == [1]

