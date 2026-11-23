from app.models import Event, Room, Participant, Proposal, ProposalRunner, Rating, Round, Timeslot
from app.solver_input import build_problem, eligibility


def _base_event(s):
    e = Event(name="R", participant_code="p", admin_code="a"); s.add(e); s.flush()
    room = Room(event_id=e.id, name="Library", capacity=10); s.add(room); s.flush()
    rnd = Round(event_id=e.id, name="Round 1", min_interest=1)
    s.add(rnd); s.flush()
    ts = Timeslot(round_id=rnd.id, name="Slot A", position=0); s.add(ts); s.flush()
    return e, room, rnd, ts


def _participant(s, e, name):
    u = Participant(event_id=e.id, display_name=name); s.add(u); s.flush()
    return u


def test_only_active_become_candidates(session):
    s = session
    e, room, rnd, ts = _base_event(s)
    u = _participant(s, e, "Sam")
    active = Proposal(event_id=e.id, round_id=rnd.id, title="Active", created_by=u.id, status="active")
    retired = Proposal(event_id=e.id, round_id=rnd.id, title="Retired", created_by=u.id, status="retired")
    s.add_all([active, retired]); s.flush()
    host = _participant(s, e, "Host")
    s.add_all([ProposalRunner(proposal_id=active.id, participant_id=host.id),
               ProposalRunner(proposal_id=retired.id, participant_id=host.id)])
    s.add(Rating(participant_id=u.id, proposal_id=active.id, stars=5))
    s.add(Rating(participant_id=u.id, proposal_id=retired.id, stars=5)); s.flush()
    p = build_problem(s, rnd.id)
    assert {c.id for c in p.candidates} == {active.id}


def test_every_room_is_in_every_slot(session):
    s = session
    e, room, rnd, ts = _base_event(s)
    den = Room(event_id=e.id, name="Den", capacity=4); s.add(den); s.flush()
    s.add(Timeslot(round_id=rnd.id, name="Slot B", position=1)); s.flush()
    p = build_problem(s, rnd.id)
    assert [sl.rooms for sl in p.slots] == [{room.id: 10, den.id: 4}] * 2


def test_presence_requires_engagement(session):
    s = session
    e, room, rnd, ts = _base_event(s)
    engaged = _participant(s, e, "Eng")
    lurker = _participant(s, e, "Lurk")
    a = Proposal(event_id=e.id, round_id=rnd.id, title="A", created_by=engaged.id); s.add(a); s.flush()
    s.add(Rating(participant_id=engaged.id, proposal_id=a.id, stars=5))
    s.add(Rating(participant_id=lurker.id, proposal_id=a.id, stars=2))  # not engaged
    s.flush()
    p = build_problem(s, rnd.id)
    assert engaged.id in p.present
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


def test_session_without_host_never_runs_unless_forced(session):
    # enough interest, but nobody runs it: not a candidate. A pinned (forced) one stays.
    s = session
    e, room, rnd, ts = _base_event(s)
    u = _participant(s, e, "Fan")
    a = Proposal(event_id=e.id, round_id=rnd.id, title="Orphan", created_by=u.id); s.add(a); s.flush()
    s.add(Rating(participant_id=u.id, proposal_id=a.id, stars=5)); s.flush()
    el = eligibility(s, rnd.id)[a.id]
    assert (el.interested, el.hosted, el.eligible) == (1, False, False)
    assert all(c.id != a.id for c in build_problem(s, rnd.id).candidates)
    assert any(c.id == a.id for c in build_problem(
        s, rnd.id, force_include=frozenset({a.id})).candidates)


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


def test_eligibility_ignores_hosts_own_rating(session):
    # the host rates their own session 5 stars: that does not count toward the floor
    s = session
    e, room, rnd, ts = _base_event(s)
    host = _participant(s, e, "Host"); guest = _participant(s, e, "Guest")
    a = Proposal(event_id=e.id, round_id=rnd.id, title="A", created_by=host.id); s.add(a); s.flush()
    s.add(ProposalRunner(proposal_id=a.id, participant_id=host.id))
    s.add(Rating(participant_id=host.id, proposal_id=a.id, stars=5))
    s.add(Rating(participant_id=guest.id, proposal_id=a.id, stars=3)); s.flush()
    el = eligibility(s, rnd.id)[a.id]
    assert (el.interested, el.floor, el.eligible) == (1, 1, True)

