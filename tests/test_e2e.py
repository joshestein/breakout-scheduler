import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.webapp import create_app
from app.config import Settings
from app.models import Event, Room, Round, Timeslot, RoundSnapshot, Proposal


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(
        secret_key="test-secret", db_path=str(tmp_path / "e2e.db"),
        backup_dir=str(tmp_path / "bk"), backup_interval_s=9999,
        bootstrap_admin_code=None))


def _csrf(client) -> str:
    html = client.get("/").text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def _post(client, url, csrf, data):
    return client.post(url, data=data, headers={"X-CSRFToken": csrf})


def test_full_lifecycle(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)

    # create event (rooms via textarea) -> admin cookie
    r = _post(admin, "/events", csrf, {
        "name": "Retreat", "participant_code": "party", "admin_code": "boss",
        "admin_name": "Org", "rooms": "Library, 12\nDen, 8"})
    assert r.status_code == 200  # followed redirect to /admin

    sf = app.state.session_factory
    with sf() as s:
        event = s.scalar(select(Event))
        rooms = s.scalars(select(Room).where(Room.event_id == event.id)).all()
        room_ids = [rm.id for rm in rooms]

    # create round + one timeslot exposing both rooms
    _post(admin, "/admin/round", csrf, {"name": "Block 1", "lambda_fairness": 12, "min_interest": 1})
    with sf() as s:
        rnd = s.scalar(select(Round))
    admin.post("/admin/timeslot", headers={"X-CSRFToken": csrf},
               data={"round_id": str(rnd.id), "name": "14:00", "duration_minutes": "45",
                     "room_ids": [str(rid) for rid in room_ids]})
    with sf() as s:
        ts = s.scalar(select(Timeslot))

    # three participants enter, propose, rate disjointly so coverage matters
    def participant(name):
        c = TestClient(app)
        _post(c, "/enter", csrf, {"code": "party", "name": name})
        return c

    a, b, cc = participant("Alice"), participant("Bob"), participant("Cara")
    _post(a, "/propose", csrf, {"title": "AI safety"})
    _post(b, "/propose", csrf, {"title": "Governance"})
    with sf() as s:
        props = {p.title: p.id for p in s.scalars(select(Proposal))}
    # Alice & Cara want AI safety (5), Bob wants Governance (5)
    _post(a, "/rate", csrf, {"proposal_id": props["AI safety"], "stars": 5})
    _post(cc, "/rate", csrf, {"proposal_id": props["AI safety"], "stars": 5})
    _post(b, "/rate", csrf, {"proposal_id": props["Governance"], "stars": 5})

    # solve & publish
    _post(admin, "/admin/solve", csrf, {"round_id": rnd.id})
    with sf() as s:
        from app.models import Placement
        placed = s.scalars(select(Placement)).all()
        # both rooms available, high lambda -> both sessions run so everyone covered
        assert len({p.proposal_id for p in placed}) == 2

    r = _post(admin, "/admin/publish", csrf, {"round_id": rnd.id})
    assert r.status_code == 200
    with sf() as s:
        assert s.scalar(select(Round)).status == "published"
        assert s.scalar(select(RoundSnapshot)) is not None

    # start the clock, then a participant sees the published grid with a highlight
    _post(admin, "/admin/timeslot-start", csrf, {"round_id": rnd.id, "timeslot_id": ts.id})
    pub = a.get("/published").text
    assert "AI safety" in pub and "→ you" in pub
    # countdown timestamp must be UTC-explicit so browsers don't misparse as local
    assert "+00:00" in pub

    # archive lists the published round
    arc = admin.get("/admin/archive").text
    assert "Block 1" in arc


def test_participant_cannot_reach_admin(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Library, 10"})
    part = TestClient(app)
    _post(part, "/enter", csrf, {"code": "p", "name": "Sam"})
    resp = part.get("/admin", follow_redirects=False)
    assert resp.status_code == 403


def test_cross_event_idor_blocked(app):
    csrf = _csrf(TestClient(app))

    # Event A with admin + a proposal
    adminA = TestClient(app)
    _post(adminA, "/events", csrf, {"name": "A", "participant_code": "pa", "admin_code": "aa",
                                    "admin_name": "OrgA", "rooms": "R, 10"})
    _post(adminA, "/admin/round", csrf, {"name": "RA", "lambda_fairness": 3, "min_interest": 1})
    partA = TestClient(app)
    _post(partA, "/enter", csrf, {"code": "pa", "name": "Ann"})
    _post(partA, "/propose", csrf, {"title": "Secret A topic"})

    sf = app.state.session_factory
    with sf() as s:
        a_prop = s.scalar(select(Proposal)).id
        a_round = s.scalar(select(Round)).id
        eventA_id = s.scalar(select(Event)).id

    # Event B with its own admin + participant
    adminB = TestClient(app)
    _post(adminB, "/events", csrf, {"name": "B", "participant_code": "pb", "admin_code": "ab",
                                    "admin_name": "OrgB", "rooms": "R, 10"})
    partB = TestClient(app)
    _post(partB, "/enter", csrf, {"code": "pb", "name": "Bo"})

    # B's participant cannot rate A's proposal
    r = partB.post("/rate", data={"proposal_id": a_prop, "stars": 5},
                   headers={"X-CSRFToken": csrf}, follow_redirects=False)
    assert r.status_code == 404
    # B's admin cannot publish A's round
    r = adminB.post("/admin/publish", data={"round_id": a_round},
                    headers={"X-CSRFToken": csrf}, follow_redirects=False)
    assert r.status_code == 404
    # B's admin cannot retire A's proposal
    r = adminB.post("/admin/retire", data={"round_id": a_round, "proposal_id": a_prop},
                    headers={"X-CSRFToken": csrf}, follow_redirects=False)
    assert r.status_code == 404
    # B's admin cannot force-place using A's room (room IDOR)
    from app.models import Timeslot
    _post(adminB, "/admin/round", csrf, {"name": "RB"})
    with sf() as s:
        a_room = s.scalar(select(Room).where(Room.event_id == eventA_id)).id  # A's room
        b_round = s.scalar(select(Round).where(Round.event_id != eventA_id)).id
        b_room = s.scalar(select(Room).where(Room.event_id != eventA_id)).id
    adminB.post("/admin/timeslot", headers={"X-CSRFToken": csrf},
                data={"round_id": b_round, "name": "x", "duration_minutes": "45",
                      "room_ids": [str(b_room)]})
    with sf() as s:
        b_ts = s.scalar(select(Timeslot).where(Timeslot.round_id == b_round)).id
    r = adminB.post("/admin/force-place", headers={"X-CSRFToken": csrf},
                    data={"round_id": b_round, "timeslot_id": b_ts, "room_id": a_room,
                          "proposal_id": a_prop}, follow_redirects=False)
    assert r.status_code == 404
    # B's admin cannot rename A's proposal (proposal-edit IDOR)
    r = adminB.post("/admin/proposal-edit", headers={"X-CSRFToken": csrf},
                    data={"round_id": a_round, "proposal_id": a_prop, "title": "hijacked"},
                    follow_redirects=False)
    assert r.status_code == 404
    # cross-round placement is rejected (proposal A into B's grid — different rounds)
    r = adminB.post("/admin/force-place", headers={"X-CSRFToken": csrf},
                    data={"round_id": b_round, "timeslot_id": b_ts, "room_id": b_room,
                          "proposal_id": a_prop}, follow_redirects=False)
    assert r.status_code in (400, 404)   # 404 (proposal not in B's event) blocks it first


def test_merge_cycle_returns_400_not_500(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "B1"})
    sf = app.state.session_factory
    with sf() as s:
        rid = s.scalar(select(Round)).id
    _post(admin, "/admin/add-session", csrf, {"round_id": rid, "title": "A"})
    _post(admin, "/admin/add-session", csrf, {"round_id": rid, "title": "B"})
    with sf() as s:
        ids = [p.id for p in s.scalars(select(Proposal))]
    _post(admin, "/admin/merge", csrf, {"round_id": rid, "source_id": ids[1], "target_id": ids[0]})
    # now merging A into B would create a cycle -> service raises ValueError -> 400
    r = admin.post("/admin/merge", data={"round_id": rid, "source_id": ids[0], "target_id": ids[1]},
                   headers={"X-CSRFToken": csrf}, follow_redirects=False)
    assert r.status_code == 400


def test_htmx_partial_and_diagnostics(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Library, 10"})
    _post(admin, "/admin/round", csrf, {"name": "B1", "lambda_fairness": 5, "min_interest": 1})
    with app.state.session_factory() as s:
        rid = s.scalar(select(Round)).id
        room_id = s.scalar(select(Room)).id

    # rooms exist but no timeslot yet -> guided step 2 is shown
    assert "Add your first timeslot" in admin.get(f"/admin?round_id={rid}").text

    admin.post("/admin/timeslot", headers={"X-CSRFToken": csrf},
               data={"round_id": rid, "name": "14:00", "duration_minutes": "45",
                     "room_ids": [str(room_id)]})

    # an htmx POST returns the console *partial* (no full HTML doc), not a redirect
    r = admin.post("/admin/add-session", data={"round_id": rid, "title": "Lonely topic"},
                   headers={"X-CSRFToken": csrf, "HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="console-main"' in r.text and "<!doctype" not in r.text.lower()
    # timeslot exists + session added but unrated -> diagnostic says nothing is eligible
    assert "No sessions are eligible" in r.text


def test_join_link_and_state_aware_default(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "Retreat", "participant_code": "joinme",
                                   "admin_code": "boss", "admin_name": "Org", "rooms": "Lib, 10"})
    sf = app.state.session_factory
    with sf() as s:
        rid = s.scalar(select(Round).where(Round.event_id == s.scalar(select(Event)).id)) if False else None
    # join link pre-fills the code (just a name field), and offers public schedule
    jr = TestClient(app).get("/j/joinme")
    assert jr.status_code == 200
    assert 'value="joinme"' in jr.text and "Retreat" in jr.text

    # public schedule view needs no sign-in
    assert TestClient(app).get("/s/joinme").status_code == 200

    # set up a round, a session, a rating, solve + publish
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    with sf() as s:
        rid = s.scalar(select(Round)).id
        room_id = s.scalar(select(Room)).id
    admin.post("/admin/timeslot", headers={"X-CSRFToken": csrf},
               data={"round_id": rid, "name": "14:00", "room_ids": [str(room_id)]})
    part = TestClient(app)
    _post(part, "/enter", csrf, {"code": "joinme", "name": "Sam"})
    _post(part, "/propose", csrf, {"title": "AI safety"})
    with sf() as s:
        pid = s.scalar(select(Proposal)).id
    _post(part, "/rate", csrf, {"proposal_id": pid, "stars": 5})
    _post(admin, "/admin/solve", csrf, {"round_id": rid})
    _post(admin, "/admin/publish", csrf, {"round_id": rid})

    # with a published round and no new collecting proposals, entering lands on the schedule
    p2 = TestClient(app)
    r = p2.post("/enter", data={"code": "joinme", "name": "Pat"},
                headers={"X-CSRFToken": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/published"

    # open a NEW round and add a proposal -> default flips back to propose/rate
    _post(admin, "/admin/round", csrf, {"name": "R2"})
    with sf() as s:
        r2 = s.scalars(select(Round)).all()[-1].id
    _post(admin, "/admin/add-session", csrf, {"round_id": r2, "title": "Round 2 topic"})
    p3 = TestClient(app)
    r = p3.post("/enter", data={"code": "joinme", "name": "Lee"},
                headers={"X-CSRFToken": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/app"


def test_public_routes_reject_admin_code_and_render(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "pp", "admin_code": "aa",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    c = TestClient(app)
    # admin code must NOT resolve on public/join routes (no oracle)
    assert c.get("/s/aa", follow_redirects=False).status_code == 404
    assert c.get("/j/aa", follow_redirects=False).status_code == 404
    # participant code works
    assert c.get("/s/pp").status_code == 200
    assert c.get("/j/pp").status_code == 200
    assert c.get("/present/pp").status_code == 200
    # projector QR page renders for the organizer
    assert admin.get("/admin/qr").status_code == 200


def test_default_secret_blocked_in_prod():
    from app.webapp import create_app
    from app.config import Settings
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        create_app(Settings(secret_key="dev-insecure-key-change-me", db_path=":memory:",
                            backup_dir="/tmp/x", backup_interval_s=999,
                            bootstrap_admin_code=None, cookie_secure=True))


def test_room_timeslot_edit_delete_routes(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10\nDen, 8"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    with sf() as s:
        rid = s.scalar(select(Round)).id
        rooms = [r.id for r in s.scalars(select(Room))]
    admin.post("/admin/timeslot", headers={"X-CSRFToken": csrf},
               data={"round_id": rid, "name": "14:00", "room_ids": [str(rooms[0])]})
    with sf() as s:
        ts = s.scalar(select(__import__("app.models", fromlist=["Timeslot"]).Timeslot)).id

    assert _post(admin, "/admin/room-edit", csrf,
                 {"round_id": rid, "room_id": rooms[0], "name": "Library", "capacity": 20}).status_code == 200
    assert _post(admin, "/admin/timeslot-edit", csrf,
                 {"round_id": rid, "timeslot_id": ts, "name": "2pm", "duration_minutes": 60}).status_code == 200
    assert _post(admin, "/admin/room-delete", csrf, {"round_id": rid, "room_id": rooms[1]}).status_code == 200
    with sf() as s:
        from app.models import Room as _R
        assert s.get(_R, rooms[1]) is None
        assert s.scalar(select(Room).where(Room.id == rooms[0])).capacity == 20

    # delete the timeslot last
    assert _post(admin, "/admin/timeslot-delete", csrf, {"round_id": rid, "timeslot_id": ts}).status_code == 200


def test_dedup_by_role_and_own_proposal_edit(app):
    from app.models import Participant
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory

    # same name + same (participant) role dedups — re-login / 2nd device ≠ new person
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    c2 = TestClient(app); _post(c2, "/enter", csrf, {"code": "p", "name": "Sam"})
    with sf() as s:
        sams = s.scalars(select(Participant).where(
            Participant.display_name == "Sam", Participant.is_admin.is_(False))).all()
    assert len(sams) == 1

    # typing the admin's name with the PARTICIPANT code grants NO admin (distinct non-admin row)
    cimp = TestClient(app); _post(cimp, "/enter", csrf, {"code": "p", "name": "Org"})
    with sf() as s:
        orgs = s.scalars(select(Participant).where(Participant.display_name == "Org")).all()
        admins = [o for o in orgs if o.is_admin]
        assert len(admins) == 1                       # only the real organizer is admin
        assert any(not o.is_admin for o in orgs)      # the impostor is a plain participant
    # ...and they hold a participant-scoped token, so the console forbids them (403,
    # not a generic redirect — they ARE authenticated, just not an admin)
    assert cimp.get("/admin", follow_redirects=False).status_code == 403

    # Sam proposes; can edit and delete their own
    _post(c1, "/propose", csrf, {"title": "Mine"})
    with sf() as s:
        pid = s.scalar(select(Proposal).where(Proposal.title == "Mine")).id
    assert _post(c1, "/propose-edit", csrf, {"proposal_id": pid, "title": "Mine v2"}).status_code == 200
    with sf() as s:
        assert s.get(Proposal, pid).title == "Mine v2"
    # a different participant cannot edit Sam's proposal
    pat = TestClient(app); _post(pat, "/enter", csrf, {"code": "p", "name": "Pat"})
    r = pat.post("/propose-edit", data={"proposal_id": pid, "title": "hijack"},
                 headers={"X-CSRFToken": csrf}, follow_redirects=False)
    assert r.status_code == 403
    # append-only auto-pull endpoint works and never re-sends already-shown rows
    assert pat.get("/app/rate-since?after=0").status_code == 200
    assert "Mine v2" not in pat.get("/app/rate-since?after=%d" % pid).text
    # admin can rename any proposal
    rid = app_round_id(sf)
    assert _post(admin, "/admin/proposal-edit", csrf,
                 {"round_id": rid, "proposal_id": pid, "title": "Renamed by org"}).status_code == 200
    with sf() as s:
        assert s.get(Proposal, pid).title == "Renamed by org"
    # owner deletes their own
    assert _post(c1, "/propose-delete", csrf, {"proposal_id": pid}).status_code == 200
    with sf() as s:
        assert s.get(Proposal, pid).status == "retired"


def app_round_id(sf):
    with sf() as s:
        return s.scalar(select(Round).order_by(Round.id.desc())).id


def test_schedule_markdown_export(app):
    from app.services import rounds as RD
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10\nDen, 8"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    # a participant proposes two sessions
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    _post(c1, "/propose", csrf, {"title": "AI safety"})
    _post(c1, "/propose", csrf, {"title": "Governance"})
    # timeslot spanning both rooms, then place each proposal
    _post(admin, "/admin/timeslot", csrf, {"round_id": rid, "name": "14:00",
                                           "duration_minutes": "45"})
    with sf() as s:
        ts = s.scalar(select(Timeslot).where(Timeslot.round_id == rid))
        rooms = {r.name: r.id for r in s.scalars(select(Room))}
        props = {p.title: p.id for p in s.scalars(select(Proposal))}
        from app.models import Placement
        s.add(Placement(timeslot_id=ts.id, room_id=rooms["Lib"], proposal_id=props["AI safety"]))
        s.add(Placement(timeslot_id=ts.id, room_id=rooms["Den"], proposal_id=props["Governance"]))
        s.commit()
        md = RD.schedule_markdown(s, rid)
    # block header + room-sorted bullet lines, paste-ready for Slack/Discord
    assert md.startswith("14:00 Breakouts")
    assert "- Den: Governance" in md
    assert "- Lib: AI safety" in md
    assert md.index("- Den") < md.index("- Lib")   # sorted by room name


def test_propose_with_host_caps(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    # optional caps flow through the propose form
    _post(c1, "/propose", csrf, {"title": "Workshop", "min_attendees": "4", "max_attendees": "8"})
    with sf() as s:
        p = s.scalar(select(Proposal).where(Proposal.title == "Workshop"))
        assert p.min_interest_override == 4
        assert p.max_attendees == 8
    # an impossible floor above the cap is clamped, not rejected
    _post(c1, "/propose", csrf, {"title": "Tiny", "min_attendees": "20", "max_attendees": "5"})
    with sf() as s:
        p = s.scalar(select(Proposal).where(Proposal.title == "Tiny"))
        assert p.min_interest_override == 5 and p.max_attendees == 5
    # blank caps stay unset
    _post(c1, "/propose", csrf, {"title": "Plain"})
    with sf() as s:
        p = s.scalar(select(Proposal).where(Proposal.title == "Plain"))
        assert p.min_interest_override is None and p.max_attendees is None


def test_stale_cb_cookie_rejected_over_http(app):
    # a correctly-signed cookie whose cb (created-at) no longer matches the row must
    # NOT silently authenticate (guards against id-reuse impersonation after a rebuild).
    from app.models import Participant
    from datetime import timedelta
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    c = TestClient(app); _post(c, "/enter", csrf, {"code": "p", "name": "Sam"})
    assert c.get("/app", follow_redirects=False).status_code == 200
    with sf() as s:
        me = s.scalar(select(Participant).where(Participant.display_name == "Sam",
                                                Participant.is_admin.is_(False)))
        me.created_at = me.created_at + timedelta(seconds=5)   # cb no longer matches
        s.commit()
    r = c.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_rate_since_returns_only_newer(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    for t in ("First", "Second", "Third"):
        _post(c1, "/propose", csrf, {"title": t})
    with sf() as s:
        ids = {p.title: p.id for p in s.scalars(select(Proposal))}
    body = c1.get("/app/rate-since?after=%d" % ids["Second"]).text
    assert "Third" in body                       # strictly newer rows are appended
    assert "First" not in body and "Second" not in body   # already-shown rows never resent


def test_cross_round_placement_rejected_same_event(app):
    # same event, but the proposal is from a different round than the target timeslot
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    r1 = app_round_id(sf)
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    _post(c1, "/propose", csrf, {"title": "R1 topic"})
    with sf() as s:
        p_r1 = s.scalar(select(Proposal).where(Proposal.title == "R1 topic")).id
        room = s.scalar(select(Room)).id
    _post(admin, "/admin/round", csrf, {"name": "R2"})
    r2 = app_round_id(sf)
    _post(admin, "/admin/timeslot", csrf, {"round_id": r2, "name": "x",
                                           "duration_minutes": "45", "room_ids": [str(room)]})
    with sf() as s:
        ts2 = s.scalar(select(Timeslot).where(Timeslot.round_id == r2)).id
    r = admin.post("/admin/force-place", headers={"X-CSRFToken": csrf},
                   data={"round_id": r2, "timeslot_id": ts2, "room_id": room, "proposal_id": p_r1},
                   follow_redirects=False)
    assert r.status_code == 400


def test_description_optional_and_admin_toggle(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    # descriptions on by default → stored
    _post(c1, "/propose", csrf, {"title": "With desc", "description": "about widgets"})
    with sf() as s:
        assert s.scalar(select(Proposal).where(Proposal.title == "With desc")).description == "about widgets"
    # admin disables descriptions for the event
    _post(admin, "/admin/event-descriptions", csrf, {"round_id": rid, "enabled": "0"})
    _post(c1, "/propose", csrf, {"title": "No desc", "description": "should be dropped"})
    with sf() as s:
        assert s.scalar(select(Proposal).where(Proposal.title == "No desc")).description is None


def test_no_interest_rater_shown_in_console(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    _post(admin, "/admin/timeslot", csrf, {"round_id": rid, "name": "A", "duration_minutes": "45"})
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    _post(c1, "/propose", csrf, {"title": "Topic"})
    with sf() as s:
        pid = s.scalar(select(Proposal).where(Proposal.title == "Topic")).id
    pat = TestClient(app); _post(pat, "/enter", csrf, {"code": "p", "name": "Pat"})
    _post(pat, "/rate", csrf, {"proposal_id": pid, "stars": 2})   # "rather not" — wants nothing
    html = admin.get("/admin?round_id=%d" % rid).text
    assert "didn't find any sessions interesting" in html
    assert "Pat" in html


def test_published_view_frozen_until_republish(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    with sf() as s:
        room = s.scalar(select(Room)).id
    _post(admin, "/admin/timeslot", csrf, {"round_id": rid, "name": "14:00",
                                           "duration_minutes": "45", "room_ids": [str(room)]})
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    _post(c1, "/propose", csrf, {"title": "Original"})
    with sf() as s:
        pid = s.scalar(select(Proposal).where(Proposal.title == "Original")).id
    _post(c1, "/rate", csrf, {"proposal_id": pid, "stars": 5})
    _post(admin, "/admin/solve", csrf, {"round_id": rid})
    _post(admin, "/admin/publish", csrf, {"round_id": rid})
    assert "Original" in TestClient(app).get("/s/p").text
    # admin renames after publishing — attendees must NOT see it change yet
    _post(admin, "/admin/proposal-edit", csrf, {"round_id": rid, "proposal_id": pid, "title": "Renamed"})
    page = TestClient(app).get("/s/p").text
    assert "Original" in page and "Renamed" not in page
    # the console warns about the drift
    assert "changes since you published" in admin.get("/admin?round_id=%d" % rid).text
    # re-publishing pushes the change live
    _post(admin, "/admin/publish", csrf, {"round_id": rid})
    assert "Renamed" in TestClient(app).get("/s/p").text


def test_bad_csrf_rejected(app):
    admin = TestClient(app)
    resp = admin.post("/events", data={"name": "X", "participant_code": "p",
                                       "admin_code": "a"}, headers={"X-CSRFToken": "bogus"})
    assert resp.status_code == 400
