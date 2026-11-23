import pytest

from app.models import (Event, Room, Participant, Proposal, ProposalRunner, Rating,
                        Round, Timeslot, TimeslotRoom, Placement)
from app.services import proposals as P
from app.services import ratings as R
from app.services import rounds as RD
from app.services.backup import backup_once


def _event(s):
    e = Event(name="R", participant_code="p", admin_code="a"); s.add(e); s.flush()
    return e


def _p(s, e, name):
    u = Participant(event_id=e.id, display_name=name); s.add(u); s.flush()
    return u


# ---- proposals -------------------------------------------------------------

def test_propose_adds_proposer_as_runner(session):
    e = _event(session); u = _p(session, e, "Sam")
    prop = P.propose(session, event_id=e.id, title="  AI safety  ", created_by=u.id)
    assert prop.title == "AI safety"
    runners = session.query(ProposalRunner).filter_by(proposal_id=prop.id).all()
    assert [r.participant_id for r in runners] == [u.id]


def test_merge_is_non_destructive_and_reversible(session):
    e = _event(session); u = _p(session, e, "U1")
    a = P.propose(session, event_id=e.id, title="A", created_by=u.id)
    b = P.propose(session, event_id=e.id, title="B", created_by=u.id)
    R.set_rating(session, participant_id=u.id, proposal_id=b.id, stars=4)
    n_ratings = session.query(Rating).count()
    n_runners = session.query(ProposalRunner).count()
    P.merge(session, source_id=b.id, target_id=a.id)
    assert session.get(Proposal, b.id).merged_into == a.id
    assert session.get(Proposal, b.id).status == "retired"
    # nothing mutated
    assert session.query(Rating).count() == n_ratings
    assert session.query(ProposalRunner).count() == n_runners
    P.unmerge(session, b.id)
    assert session.get(Proposal, b.id).merged_into is None
    assert session.get(Proposal, b.id).status == "active"


def test_load_participant_rejects_stale_cookie(session):
    from app.deps import load_participant, participant_cb
    from app.auth import SessionToken
    s = session; e = _event(s); u = _p(s, e, "X")
    assert load_participant(s, SessionToken(u.id, "participant", participant_cb(u))) is u
    # id reused for a different person (DB rebuilt) -> cb mismatch -> rejected
    assert load_participant(s, SessionToken(u.id, "participant", participant_cb(u) + 1)) is None
    # legacy cookie without cb is grandfathered (don't disable existing logins)
    assert load_participant(s, SessionToken(u.id, "participant", None)) is u


def test_delete_room_cascades(session):
    s = session
    e, rooms, rnd, ts = _round_with_two_rooms(s)
    r1 = rooms[0]
    u = _p(s, e, "U")
    a = P.propose(s, event_id=e.id, title="A", created_by=u.id)
    s.add(Placement(timeslot_id=ts.id, room_id=r1.id, proposal_id=a.id)); s.flush()
    RD.delete_room(s, r1.id)
    assert s.get(Room, r1.id) is None
    assert s.query(TimeslotRoom).filter_by(room_id=r1.id).count() == 0
    assert s.query(Placement).filter_by(room_id=r1.id).count() == 0


def test_delete_timeslot_cascades(session):
    s = session
    e, rooms, rnd, ts = _round_with_two_rooms(s)
    u = _p(s, e, "U")
    a = P.propose(s, event_id=e.id, title="A", created_by=u.id)
    s.add(Placement(timeslot_id=ts.id, room_id=rooms[0].id, proposal_id=a.id)); s.flush()
    RD.delete_timeslot(s, ts.id)
    assert s.get(Timeslot, ts.id) is None
    assert s.query(Placement).filter_by(timeslot_id=ts.id).count() == 0
    assert s.query(TimeslotRoom).filter_by(timeslot_id=ts.id).count() == 0


def test_cross_round_merge_rejected(session):
    s = session; e = _event(s)
    r1 = RD.create_round(s, event_id=e.id, name="R1")
    u = _p(s, e, "U")
    a = P.propose(s, event_id=e.id, title="A", created_by=u.id)   # -> r1
    r2 = RD.create_round(s, event_id=e.id, name="R2")
    b = P.propose(s, event_id=e.id, title="B", created_by=u.id)   # -> r2
    with pytest.raises(ValueError):
        P.merge(s, source_id=a.id, target_id=b.id)                # different rounds


def test_current_round_id_excludes_published(session):
    from app.services.proposals import current_round_id
    s = session; e = _event(s)
    r1 = RD.create_round(s, event_id=e.id, name="R1")
    assert current_round_id(s, e.id) == r1.id
    r1.status = "published"; s.flush()
    assert current_round_id(s, e.id) is None   # never targets a published round


def test_merge_cycle_rejected(session):
    e = _event(session); u = _p(session, e, "U1")
    a = P.propose(session, event_id=e.id, title="A", created_by=u.id)
    b = P.propose(session, event_id=e.id, title="B", created_by=u.id)
    P.merge(session, source_id=b.id, target_id=a.id)
    with pytest.raises(ValueError):
        P.merge(session, source_id=a.id, target_id=b.id)


# ---- ratings ---------------------------------------------------------------

def test_set_rating_upserts(session):
    e = _event(session); u = _p(session, e, "U1")
    a = P.propose(session, event_id=e.id, title="A", created_by=u.id)
    R.set_rating(session, participant_id=u.id, proposal_id=a.id, stars=3)
    R.set_rating(session, participant_id=u.id, proposal_id=a.id, stars=5)
    rows = session.query(Rating).filter_by(participant_id=u.id, proposal_id=a.id).all()
    assert len(rows) == 1 and rows[0].stars == 5


def test_set_rating_validates_range(session):
    e = _event(session); u = _p(session, e, "U1")
    a = P.propose(session, event_id=e.id, title="A", created_by=u.id)
    with pytest.raises(ValueError):
        R.set_rating(session, participant_id=u.id, proposal_id=a.id, stars=6)


# ---- rounds ----------------------------------------------------------------

def _round_with_two_rooms(session):
    e = _event(session)
    r1 = Room(event_id=e.id, name="Library", capacity=10)
    r2 = Room(event_id=e.id, name="Den", capacity=10)
    session.add_all([r1, r2]); session.flush()
    rnd = RD.create_round(session, event_id=e.id, name="Round 1", lambda_fairness=10.0)
    ts = RD.add_timeslot(session, round_id=rnd.id, name="Slot A", position=0,
                         room_ids=[r1.id, r2.id])
    return e, [r1, r2], rnd, ts


def test_solve_apply_and_publish_validation(session):
    e, rooms, rnd, ts = _round_with_two_rooms(session)
    u1 = _p(session, e, "U1"); u2 = _p(session, e, "U2")
    a = P.propose(session, event_id=e.id, title="A", created_by=u1.id)
    b = P.propose(session, event_id=e.id, title="B", created_by=u2.id)
    R.set_rating(session, participant_id=u1.id, proposal_id=a.id, stars=5)
    R.set_rating(session, participant_id=u2.id, proposal_id=b.id, stars=5)
    result = RD.solve_round(session, rnd.id)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    RD.apply_result(session, rnd.id, result)
    placed = session.query(Placement).filter(Placement.timeslot_id == ts.id).count()
    assert placed >= 1
    snap = RD.publish(session, rnd.id)
    assert snap.id is not None
    assert session.get(Round, rnd.id).status == "published"


def test_publish_rejects_room_not_in_timeslot(session):
    e, rooms, rnd, ts = _round_with_two_rooms(session)
    other = Room(event_id=e.id, name="Hallway", capacity=5)
    session.add(other); session.flush()
    u1 = _p(session, e, "U1")
    a = P.propose(session, event_id=e.id, title="A", created_by=u1.id)
    # force a placement into a room not available this slot
    session.add(Placement(timeslot_id=ts.id, room_id=other.id, proposal_id=a.id))
    session.flush()
    with pytest.raises(ValueError):
        RD.publish(session, rnd.id)


def test_unpublish_blocked_after_clock_starts(session):
    e, rooms, rnd, ts = _round_with_two_rooms(session)
    u1 = _p(session, e, "U1")
    a = P.propose(session, event_id=e.id, title="A", created_by=u1.id)
    R.set_rating(session, participant_id=u1.id, proposal_id=a.id, stars=5)
    RD.apply_result(session, rnd.id, RD.solve_round(session, rnd.id))
    RD.publish(session, rnd.id)
    RD.start_timeslot(session, ts.id)
    with pytest.raises(ValueError):
        RD.unpublish(session, rnd.id)


def test_propose_without_runner_adds_no_runner(session):
    e = _event(session); u = _p(session, e, "Org")
    prop = P.propose(session, event_id=e.id, title="Topic", created_by=u.id, runner=False)
    assert session.query(ProposalRunner).filter_by(proposal_id=prop.id).count() == 0


def test_set_timeslot_rooms_replaces_and_prunes_placements(session):
    from app.models import Placement, TimeslotRoom
    e, rooms, rnd, ts = _round_with_two_rooms(session)   # ts exposes rooms[0], rooms[1]
    r1, r2 = rooms
    u = _p(session, e, "U1")
    a = P.propose(session, event_id=e.id, title="A", created_by=u.id)
    # place A in r2, then remove r2 from the timeslot -> placement must be dropped
    session.add(Placement(timeslot_id=ts.id, room_id=r2.id, proposal_id=a.id)); session.flush()
    RD.set_timeslot_rooms(session, timeslot_id=ts.id, room_ids=[r1.id])
    room_ids = {tr.room_id for tr in session.query(TimeslotRoom).filter_by(timeslot_id=ts.id)}
    assert room_ids == {r1.id}
    assert session.query(Placement).filter_by(timeslot_id=ts.id).count() == 0


def test_rounds_are_isolated_proposals_dont_carry(session):
    from app.solver_input import build_problem
    s = session; e = _event(s)
    r1 = RD.create_round(s, event_id=e.id, name="R1")
    u = _p(s, e, "U")
    a = P.propose(s, event_id=e.id, title="A", created_by=u.id)   # auto -> r1 (latest)
    assert a.round_id == r1.id
    R.set_rating(s, participant_id=u.id, proposal_id=a.id, stars=5)
    r2 = RD.create_round(s, event_id=e.id, name="R2")
    b = P.propose(s, event_id=e.id, title="B", created_by=u.id)   # now -> r2 (latest)
    assert b.round_id == r2.id
    # A is a candidate in its own round, but does NOT leak into the new round
    assert any(c.id == a.id for c in build_problem(s, r1.id).candidates)
    assert all(c.id != a.id for c in build_problem(s, r2.id).candidates)


def test_set_present_drops_runner_from_solve(session):
    from app.solver_input import build_problem
    e, rooms, rnd, ts = _round_with_two_rooms(session)
    runner = _p(session, e, "Runner")
    a = P.propose(session, event_id=e.id, title="A", created_by=runner.id)
    # before marking absent: runner is present and is a runner of the candidate
    before = build_problem(session, rnd.id)
    assert runner.id in before.present
    assert any(runner.id in c.runners for c in before.candidates)
    # mark absent -> dropped from present AND from the candidate's runner set
    RD.set_present(session, round_id=rnd.id, participant_id=runner.id, present=False)
    after = build_problem(session, rnd.id)
    assert runner.id not in after.present
    assert all(runner.id not in c.runners for c in after.candidates)


# ---- backup ----------------------------------------------------------------

def test_backup_once_creates_file(tmp_path):
    import sqlite3
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db); conn.execute("CREATE TABLE t(x)"); conn.commit(); conn.close()
    dest = backup_once(str(db), str(tmp_path / "backups"), stamp="20260602-120000")
    import os
    assert os.path.exists(dest)
