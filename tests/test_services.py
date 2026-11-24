import pytest

from app.models import (Event, Room, Participant, Proposal, ProposalRunner, Rating,
                        Round, Timeslot, Placement)
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


def test_load_participant_rejects_stale_cookie(session):
    from app.deps import load_participant, participant_cb
    from app.auth import SessionToken
    s = session; e = _event(s); u = _p(s, e, "X")
    assert load_participant(s, SessionToken(u.id, "participant", participant_cb(u))) is u
    # id reused for a different person (DB rebuilt) -> cb mismatch -> rejected
    assert load_participant(s, SessionToken(u.id, "participant", participant_cb(u) + 1)) is None


def test_delete_room_cascades(session):
    s = session
    e, rooms, rnd, ts = _round_with_two_rooms(s)
    r1 = rooms[0]
    u = _p(s, e, "U")
    a = P.propose(s, event_id=e.id, title="A", created_by=u.id)
    s.add(Placement(timeslot_id=ts.id, room_id=r1.id, proposal_id=a.id)); s.flush()
    RD.delete_room(s, r1.id)
    assert s.get(Room, r1.id) is None
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


def test_input_round_excludes_published(session):
    s = session; e = _event(s)
    r1 = RD.create_round(s, event_id=e.id, name="R1")
    assert RD.input_round(s, e.id) is r1
    r1.status = "published"; s.flush()
    assert RD.input_round(s, e.id) is None   # never targets a published round
    assert RD.published_round(s, e.id) is r1


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
    rnd = RD.create_round(session, event_id=e.id, name="Round 1", min_interest=1)
    ts = RD.add_timeslot(session, round_id=rnd.id, name="Slot A", position=0)
    return e, [r1, r2], rnd, ts


def test_solve_apply_and_publish(session):
    e, rooms, rnd, ts = _round_with_two_rooms(session)
    u1 = _p(session, e, "U1"); u2 = _p(session, e, "U2")
    a = P.propose(session, event_id=e.id, title="A", created_by=u1.id)
    b = P.propose(session, event_id=e.id, title="B", created_by=u2.id)
    # each rates the other's session (a host's own rating doesn't count toward the floor)
    R.set_rating(session, participant_id=u2.id, proposal_id=a.id, stars=5)
    R.set_rating(session, participant_id=u1.id, proposal_id=b.id, stars=5)
    result = RD.solve_round(session, rnd.id)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    RD.apply_result(session, rnd.id, result)
    placed = session.query(Placement).filter(Placement.timeslot_id == ts.id).count()
    assert placed >= 1
    RD.publish(session, rnd.id)
    assert session.get(Round, rnd.id).status == "published"


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


def test_rounds_are_isolated_proposals_dont_carry(session):
    from app.solver_input import build_problem
    s = session; e = _event(s)
    r1 = RD.create_round(s, event_id=e.id, name="R1", min_interest=1)
    u = _p(s, e, "U"); v = _p(s, e, "V")
    a = P.propose(s, event_id=e.id, title="A", created_by=u.id)   # auto -> r1 (latest)
    assert a.round_id == r1.id
    R.set_rating(s, participant_id=v.id, proposal_id=a.id, stars=5)
    r2 = RD.create_round(s, event_id=e.id, name="R2")
    b = P.propose(s, event_id=e.id, title="B", created_by=u.id)   # now -> r2 (latest)
    assert b.round_id == r2.id
    # A is a candidate in its own round, but does NOT leak into the new round
    assert any(c.id == a.id for c in build_problem(s, r1.id).candidates)
    assert all(c.id != a.id for c in build_problem(s, r2.id).candidates)


# ---- backup ----------------------------------------------------------------

def test_backup_once_creates_file(tmp_path):
    import sqlite3
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db); conn.execute("CREATE TABLE t(x)"); conn.commit(); conn.close()
    dest = backup_once(str(db), str(tmp_path / "backups"), stamp="20260602-120000")
    import os
    assert os.path.exists(dest)


def test_apply_result_rejects_infeasible_and_keeps_grid(session):
    from dataclasses import replace
    e, rooms, rnd, ts = _round_with_two_rooms(session)
    u1 = _p(session, e, "U1")
    a = P.propose(session, event_id=e.id, title="A", created_by=u1.id, runner=False)
    P.set_runner(session, a.id, _p(session, e, "Host").id, True)
    R.set_rating(session, participant_id=u1.id, proposal_id=a.id, stars=5)
    good = RD.solve_round(session, rnd.id)
    RD.apply_result(session, rnd.id, good)
    before = session.query(Placement).count()
    assert before >= 1
    with pytest.raises(ValueError):
        RD.apply_result(session, rnd.id, replace(good, status="INFEASIBLE", placements=[]))
    assert session.query(Placement).count() == before
    with pytest.raises(ValueError, match="ran out of time"):
        RD.apply_result(session, rnd.id, replace(good, status="UNKNOWN", placements=[]))
    assert session.query(Placement).count() == before
