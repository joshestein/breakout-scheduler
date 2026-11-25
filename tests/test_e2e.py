import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.webapp import create_app
from app.config import Settings
from app.models import Event, Room, Round, Timeslot, Proposal, Placement, Rating


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
    _post(admin, "/admin/round", csrf, {"name": "Block 1", "min_interest": 1})
    with sf() as s:
        rnd = s.scalar(select(Round))
    admin.post("/admin/timeslot", headers={"X-CSRFToken": csrf},
               data={"round_id": str(rnd.id), "name": "14:00", "duration_minutes": "45"})
    with sf() as s:
        ts = s.scalar(select(Timeslot))

    # three participants enter, propose, rate disjointly so coverage matters
    def participant(name):
        c = TestClient(app)
        _post(c, "/enter", csrf, {"code": "party", "name": name})
        return c

    a, b, cc, d = participant("Alice"), participant("Bob"), participant("Cara"), participant("Dan")
    _post(a, "/propose", csrf, {"title": "AI safety"})
    _post(b, "/propose", csrf, {"title": "Governance"})
    with sf() as s:
        props = {p.title: p.id for p in s.scalars(select(Proposal))}
    # Cara wants AI safety (Alice hosts), Dan wants Governance (Bob hosts)
    _post(a, "/rate", csrf, {"proposal_id": props["AI safety"], "stars": 5})
    _post(cc, "/rate", csrf, {"proposal_id": props["AI safety"], "stars": 5})
    _post(b, "/rate", csrf, {"proposal_id": props["Governance"], "stars": 5})
    _post(d, "/rate", csrf, {"proposal_id": props["Governance"], "stars": 5})

    # solve & publish
    _post(admin, "/admin/solve", csrf, {"round_id": rnd.id})
    with sf() as s:
        from app.models import Placement
        placed = s.scalars(select(Placement)).all()
        # both rooms available -> both sessions run so everyone is covered
        assert len({p.proposal_id for p in placed}) == 2

    r = _post(admin, "/admin/publish", csrf, {"round_id": rnd.id})
    assert r.status_code == 200
    with sf() as s:
        assert s.scalar(select(Round)).status == "published"

    # start the clock, then a participant sees the published grid with a highlight
    _post(admin, "/admin/timeslot-start", csrf, {"round_id": rnd.id, "timeslot_id": ts.id})
    pub = a.get("/published").text
    assert "AI safety" in pub and "Your seat" in pub
    # countdown timestamp must be UTC-explicit so browsers don't misparse as local
    assert "+00:00" in pub


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
    _post(adminA, "/admin/round", csrf, {"name": "RA", "min_interest": 1})
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
    r = adminB.post("/admin/remove", data={"round_id": a_round, "proposal_id": a_prop},
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
                data={"round_id": b_round, "name": "x", "duration_minutes": "45"})
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


def test_htmx_partial_and_diagnostics(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Library, 10"})
    _post(admin, "/admin/round", csrf, {"name": "B1", "min_interest": 1})
    with app.state.session_factory() as s:
        rid = s.scalar(select(Round)).id
        room_id = s.scalar(select(Room)).id

    # rooms exist but no timeslot yet -> guided step 2 is shown
    assert "Add your first timeslot" in admin.get(f"/admin?round_id={rid}").text

    admin.post("/admin/timeslot", headers={"X-CSRFToken": csrf},
               data={"round_id": rid, "name": "14:00", "duration_minutes": "45"})

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
               data={"round_id": rid, "name": "14:00"})
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

    # a NEW round goes live when R1 is archived -> default flips back to propose/rate
    _post(admin, "/admin/round", csrf, {"name": "R2"})
    _post(admin, "/admin/round-archive", csrf, {"round_id": rid})
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
               data={"round_id": rid, "name": "14:00"})
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


def test_add_room_and_event_level_edit_render_console(app):
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org"})
    hx = {"X-CSRFToken": csrf, "HX-Request": "true"}
    # no round yet: redirect to the console
    r = admin.post("/admin/rooms", headers=hx, data={"name": "Lib", "capacity": 10},
                   follow_redirects=False)
    assert r.status_code == 303
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    # with a round: the console partial for the latest round
    r = admin.post("/admin/rooms", headers=hx, data={"name": "Den", "capacity": 8})
    assert r.status_code == 200 and "Den" in r.text and "R1" in r.text
    with app.state.session_factory() as s:
        den = s.scalar(select(Room).where(Room.name == "Den")).id
    # an edit with no round_id falls back to the latest round
    r = admin.post("/admin/room-edit", headers=hx,
                   data={"room_id": den, "name": "Study", "capacity": 8})
    assert r.status_code == 200 and "Study" in r.text and "R1" in r.text


def test_runner_toggle_sets_host_and_top_rating(app):
    from app.models import ProposalRunner, Rating
    csrf = _csrf(TestClient(app))
    admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    with sf() as s:
        rid = s.scalar(select(Round)).id
    _post(admin, "/admin/add-session", csrf, {"round_id": rid, "title": "Topic"})
    with sf() as s:
        pid = s.scalar(select(Proposal)).id
    ann = TestClient(app)
    _post(ann, "/enter", csrf, {"code": "p", "name": "Ann"})
    # organizer-added topic: the organizer is not a host
    assert "No host yet." in ann.get("/app").text

    r = _post(ann, "/runner-toggle", csrf, {"proposal_id": pid, "on": 1})
    assert r.status_code == 200 and "Topic" in r.text and "Hosted by Ann." in r.text
    with sf() as s:
        assert s.scalar(select(ProposalRunner).where(ProposalRunner.proposal_id == pid))
        assert s.scalar(select(Rating).where(Rating.proposal_id == pid)).stars == 5

    r = _post(ann, "/runner-toggle", csrf, {"proposal_id": pid, "on": 0})
    assert r.status_code == 200 and "No host yet." in r.text
    with sf() as s:
        assert s.scalar(select(ProposalRunner).where(ProposalRunner.proposal_id == pid)) is None


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
                                           "duration_minutes": "45"})
    with sf() as s:
        ts2 = s.scalar(select(Timeslot).where(Timeslot.round_id == r2)).id
    r = admin.post("/admin/force-place", headers={"X-CSRFToken": csrf},
                   data={"round_id": r2, "timeslot_id": ts2, "room_id": room, "proposal_id": p_r1},
                   follow_redirects=False)
    assert r.status_code == 400


def test_place_by_hand_moves_replaces_and_remove_sticks(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10\nHall, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    with sf() as s:
        room_ids = [str(r.id) for r in s.scalars(select(Room))]
    _post(admin, "/admin/timeslot", csrf, {"round_id": rid, "name": "A",
                                           "duration_minutes": "45"})
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    _post(c1, "/propose", csrf, {"title": "Topic"})
    with sf() as s:
        pid = s.scalar(select(Proposal)).id
        ts = s.scalar(select(Timeslot).where(Timeslot.round_id == rid)).id
    def place(room):
        return admin.post("/admin/force-place", headers={"X-CSRFToken": csrf},
                          data={"round_id": rid, "timeslot_id": ts, "room_id": room,
                                "proposal_id": pid}, follow_redirects=False)
    def cells():
        with sf() as s:
            return sorted((p.room_id, p.proposal_id, p.pinned) for p in s.scalars(select(Placement)))
    assert place(room_ids[0]).status_code in (200, 303)
    assert cells() == [(int(room_ids[0]), pid, True)]
    # placing the same session elsewhere moves it (never two placements)
    assert place(room_ids[1]).status_code in (200, 303)
    assert cells() == [(int(room_ids[1]), pid, True)]
    # an unplaced session on a filled cell replaces it (the old one goes to the pool).
    # A different host proposes it: one host can't run both in the same slot.
    c2 = TestClient(app); _post(c2, "/enter", csrf, {"code": "p", "name": "Kim"})
    _post(c2, "/propose", csrf, {"title": "Other"})
    with sf() as s:
        other = s.scalar(select(Proposal).where(Proposal.title == "Other")).id
    admin.post("/admin/force-place", headers={"X-CSRFToken": csrf},
               data={"round_id": rid, "timeslot_id": ts, "room_id": room_ids[1],
                     "proposal_id": other}, follow_redirects=False)
    assert cells() == [(int(room_ids[1]), other, True)]
    # a placed session onto another filled cell moves there; the old one goes to the pool
    place(room_ids[0])
    assert cells() == [(int(room_ids[0]), pid, True), (int(room_ids[1]), other, True)]
    admin.post("/admin/force-place", headers={"X-CSRFToken": csrf},
               data={"round_id": rid, "timeslot_id": ts, "room_id": room_ids[0],
                     "proposal_id": other}, follow_redirects=False)
    assert cells() == [(int(room_ids[0]), other, True)]
    place(room_ids[1])
    # remove sticks: the session leaves the grid, can't be placed, and comes back on restore
    _post(admin, "/admin/remove", csrf, {"round_id": rid, "proposal_id": other})
    assert cells() == [(int(room_ids[1]), pid, True)]
    assert admin.post("/admin/force-place", headers={"X-CSRFToken": csrf},
                      data={"round_id": rid, "timeslot_id": ts, "room_id": room_ids[1],
                            "proposal_id": other}, follow_redirects=False).status_code == 400
    _post(admin, "/admin/solve", csrf, {"round_id": rid})
    assert all(p != other for _, p, _ in cells())
    _post(admin, "/admin/restore", csrf, {"round_id": rid, "proposal_id": other})
    assert admin.post("/admin/force-place", headers={"X-CSRFToken": csrf},
                      data={"round_id": rid, "timeslot_id": ts, "room_id": room_ids[0],
                            "proposal_id": other}, follow_redirects=False).status_code in (200, 303)


def test_description_is_stored(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    _post(c1, "/propose", csrf, {"title": "With desc", "description": "about widgets"})
    with sf() as s:
        assert s.scalar(select(Proposal).where(Proposal.title == "With desc")).description == "about widgets"


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
    assert "rated every session ★1–2" in html
    assert "Pat" in html


def test_published_view_shows_live_grid(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1", "min_interest": 1})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    _post(admin, "/admin/timeslot", csrf, {"round_id": rid, "name": "14:00", "duration_minutes": "45"})
    c1 = TestClient(app); _post(c1, "/enter", csrf, {"code": "p", "name": "Sam"})
    _post(c1, "/propose", csrf, {"title": "Original"})
    with sf() as s:
        pid = s.scalar(select(Proposal).where(Proposal.title == "Original")).id
    c2 = TestClient(app); _post(c2, "/enter", csrf, {"code": "p", "name": "Kim"})
    _post(c2, "/rate", csrf, {"proposal_id": pid, "stars": 5})
    _post(admin, "/admin/solve", csrf, {"round_id": rid})
    assert "Original" not in TestClient(app).get("/s/p").text   # not published yet
    _post(admin, "/admin/publish", csrf, {"round_id": rid})
    assert "Original" in TestClient(app).get("/s/p").text
    # an edit after publishing is visible at once, in the grid and the Markdown
    _post(admin, "/admin/proposal-edit", csrf, {"round_id": rid, "proposal_id": pid, "title": "Renamed"})
    page = TestClient(app).get("/s/p").text
    assert "Renamed" in page and "Original" not in page

def test_bad_csrf_rejected(app):
    admin = TestClient(app)
    resp = admin.post("/events", data={"name": "X", "participant_code": "p",
                                       "admin_code": "a"}, headers={"X-CSRFToken": "bogus"})
    assert resp.status_code == 400


def test_console_explains_cards_and_unplaced_sessions(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1", "min_interest": 1})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    _post(admin, "/admin/timeslot", csrf, {"round_id": rid, "name": "14:00", "duration_minutes": "45"})

    def person(name):
        c = TestClient(app); _post(c, "/enter", csrf, {"code": "p", "name": name}); return c

    def pid(title):
        with sf() as s:
            return s.scalar(select(Proposal).where(Proposal.title == title)).id

    ann, bob = person("Ann"), person("Bob")
    _post(ann, "/propose", csrf, {"title": "Alpha"})
    _post(ann, "/propose", csrf, {"title": "Delta"})                     # Ann hosts both
    _post(bob, "/propose", csrf, {"title": "Beta", "min_attendees": "2"})
    _post(admin, "/admin/add-session", csrf, {"round_id": rid, "title": "Gamma"})  # no host
    for name, title, stars in [("G1", "Alpha", 5), ("G2", "Alpha", 5), ("G3", "Beta", 3),
                               ("G4", "Gamma", 3), ("G5", "Gamma", 3),
                               ("G6", "Delta", 3), ("G7", "Delta", 3)]:
        _post(person(name), "/rate", csrf, {"proposal_id": pid(title), "stars": stars})
    person("Quiet")                                                       # never rates
    _post(admin, "/admin/solve", csrf, {"round_id": rid})
    html = admin.get("/admin?round_id=%d" % rid).text

    # the one room goes to Alpha: it covers Ann (host) + 2 guests, and they rated it ★5
    assert "Host: Ann" in html and "2 of 10 seats" in html and "2 at ★5" in html
    assert "1 want it, needs 2" in html                 # Beta: below its own minimum
    assert "host busy: Ann runs Alpha" in html          # Delta: its host is in Alpha
    assert "no host: nobody runs it" in html            # Gamma: nobody runs it
    assert "No host</div>" not in html
    assert "3 of 9 have a session" in html              # Ann + G1 + G2, of the 2 hosts + 7 raters
    assert "Quiet" in html and "Not rated" in html          # non-raters are listed

    # placed by hand, a session with no host stays on the grid but is flagged
    with sf() as s:
        ts_id = s.scalar(select(Timeslot).where(Timeslot.round_id == rid)).id
        room_id = s.scalar(select(Room).where(Room.name == "Lib")).id
    _post(admin, "/admin/force-place", csrf, {"round_id": rid, "timeslot_id": ts_id,
                                             "room_id": room_id, "proposal_id": pid("Gamma")})
    _post(admin, "/admin/solve", csrf, {"round_id": rid})     # pinned: Generate keeps it
    html = admin.get("/admin?round_id=%d" % rid).text
    assert "No host</div>" in html and "2 of 10 seats" in html


def test_console_badge_uses_clamped_floor(app):
    # round minimum 2, host caps the workshop at 1 guest, 1 guest is keen: it can run,
    # so the pool must not say it is below its minimum
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1"})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    host = TestClient(app); _post(host, "/enter", csrf, {"code": "p", "name": "Host"})
    _post(host, "/propose", csrf, {"title": "Tiny", "max_attendees": "1"})
    with sf() as s:
        pid = s.scalar(select(Proposal).where(Proposal.title == "Tiny")).id
    guest = TestClient(app); _post(guest, "/enter", csrf, {"code": "p", "name": "Guest"})
    _post(guest, "/rate", csrf, {"proposal_id": pid, "stars": 5})
    html = admin.get(f"/admin?round_id={rid}").text
    assert "Tiny" in html and "1 want it, needs 1" not in html and "needs 2" not in html
    assert "not picked: 1 want it" in html


def _two_room_round(app, csrf, admin, slots=("14:00",)):
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Lib, 10\nDen, 10"})
    _post(admin, "/admin/round", csrf, {"name": "R1", "min_interest": 1})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    for name in slots:
        _post(admin, "/admin/timeslot", csrf, {"round_id": rid, "name": name, "duration_minutes": "45"})
    with sf() as s:
        rooms = [r.id for r in s.scalars(select(Room).order_by(Room.id))]
        ts = [t.id for t in s.scalars(select(Timeslot).order_by(Timeslot.position))]
    return sf, rid, rooms, ts


def test_place_refuses_host_clash(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    sf, rid, rooms, ts = _two_room_round(app, csrf, admin, slots=("14:00", "15:00"))
    ann = TestClient(app); _post(ann, "/enter", csrf, {"code": "p", "name": "Ann"})
    _post(ann, "/propose", csrf, {"title": "A1"}); _post(ann, "/propose", csrf, {"title": "A2"})
    with sf() as s:
        ids = {p.title: p.id for p in s.scalars(select(Proposal))}

    def place(t, room, title):
        return admin.post("/admin/force-place", headers={"X-CSRFToken": csrf},
                          data={"round_id": rid, "timeslot_id": t, "room_id": room,
                                "proposal_id": ids[title]}, follow_redirects=False)
    assert place(ts[0], rooms[0], "A1").status_code in (200, 303)
    r = place(ts[0], rooms[1], "A2")                     # Ann can't host both at 14:00
    assert r.status_code == 409 and "Ann already hosts" in r.json()["detail"]
    assert place(ts[1], rooms[1], "A2").status_code in (200, 303)   # 15:00 is fine
    # placing a session in the cell it already has just pins it
    with sf() as s:
        a2 = s.scalar(select(Placement).where(Placement.proposal_id == ids["A2"]))
        a2.pinned = False; s.commit()
    assert place(ts[1], rooms[1], "A2").status_code in (200, 303)
    with sf() as s:
        assert s.scalar(select(Placement).where(Placement.proposal_id == ids["A2"])).pinned
    with sf() as s:
        assert {(p.timeslot_id, p.proposal_id) for p in s.scalars(select(Placement))} == {
            (ts[0], ids["A1"]), (ts[1], ids["A2"])}


def test_broken_grid_shows_banner_and_real_titles(app):
    # a grid the rules forbid (a host in two sessions at once, written straight to the
    # DB) must not show made-up counts or "?" titles
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    sf, rid, rooms, ts = _two_room_round(app, csrf, admin)
    ann = TestClient(app); _post(ann, "/enter", csrf, {"code": "p", "name": "Ann"})
    _post(ann, "/propose", csrf, {"title": "A1"}); _post(ann, "/propose", csrf, {"title": "A2"})
    with sf() as s:
        for room, p in zip(rooms, s.scalars(select(Proposal).order_by(Proposal.id))):
            s.add(Placement(timeslot_id=ts[0], room_id=room, proposal_id=p.id, pinned=True))
        s.commit()
    html = admin.get("/admin?round_id=%d" % rid).text
    assert "breaks a rule" in html and "A1" in html and "A2" in html
    assert "have a session" not in html


def test_removed_session_fans_still_counted(app):
    # Kim wanted only Gone; after Gone is removed Kim must still show up somewhere
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    sf, rid, rooms, ts = _two_room_round(app, csrf, admin)
    _post(admin, "/admin/add-session", csrf, {"round_id": rid, "title": "Gone"})
    with sf() as s:
        gone = s.scalar(select(Proposal).where(Proposal.title == "Gone")).id
    kim = TestClient(app); _post(kim, "/enter", csrf, {"code": "p", "name": "Kim"})
    _post(kim, "/rate", csrf, {"proposal_id": gone, "stars": 5})
    _post(admin, "/admin/remove", csrf, {"round_id": rid, "proposal_id": gone})
    html = admin.get("/admin?round_id=%d" % rid).text
    assert "Not rated <span>1</span>" in html and "Kim" in html


def test_published_view_shows_the_solver_seat_not_the_top_star(app):
    # Kim and Lee both rate Alpha above Beta, but Beta needs a guest to run (its host
    # Bob wants nothing else), so the solver seats one of them in Beta. That person
    # must be sent to Beta, not to their top-rated Alpha.
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _post(admin, "/events", csrf, {"name": "R", "participant_code": "p", "admin_code": "a",
                                   "admin_name": "Org", "rooms": "Big, 10\nSmall, 1"})
    _post(admin, "/admin/round", csrf, {"name": "R1", "min_interest": 1})
    sf = app.state.session_factory
    rid = app_round_id(sf)
    _post(admin, "/admin/timeslot", csrf, {"round_id": rid, "name": "14:00", "duration_minutes": "45"})

    def person(name):
        c = TestClient(app); _post(c, "/enter", csrf, {"code": "p", "name": name}); return c

    def pid(title):
        with sf() as s:
            return s.scalar(select(Proposal).where(Proposal.title == title)).id

    def seat(client):
        go = re.search(r'<li class="go">(.*?)</li>', client.get("/published").text, re.S)
        return go and re.search(r'class="what">([^<]+)', go.group(1)).group(1).strip()

    ann, bob = person("Ann"), person("Bob")
    _post(ann, "/propose", csrf, {"title": "Alpha"})
    _post(bob, "/propose", csrf, {"title": "Beta"})
    guests = [person("Kim"), person("Lee")]
    for g in guests:
        _post(g, "/rate", csrf, {"proposal_id": pid("Alpha"), "stars": 5})
        _post(g, "/rate", csrf, {"proposal_id": pid("Beta"), "stars": 4})
    _post(admin, "/admin/solve", csrf, {"round_id": rid})
    _post(admin, "/admin/publish", csrf, {"round_id": rid})

    assert sorted(seat(g) for g in guests) == ["Alpha", "Beta"]
    assert seat(ann) == "Alpha" and seat(bob) == "Beta"

    # an edit after publishing updates the seats at once
    _post(admin, "/admin/remove", csrf, {"round_id": rid, "proposal_id": pid("Beta")})
    assert seat(bob) is None
    assert "No seat was picked for you here" in bob.get("/published").text


def test_admin_code_sign_in_and_bad_code_backoff(app):
    csrf = _csrf(TestClient(app)); _two_room_round(app, csrf, TestClient(app))
    org = TestClient(app)
    assert _post(org, "/enter", csrf, {"code": "a", "name": "Org"}).url.path == "/admin"
    bad = TestClient(app)
    assert _post(bad, "/enter", csrf, {"code": "nope", "name": "X"}).status_code == 403
    assert _post(bad, "/enter", csrf, {"code": "a", "name": "X"}).status_code == 429


def _hosted_sessions(app, csrf, admin, titles):
    sf, rid, rooms, ts = _two_room_round(app, csrf, admin)
    ann = TestClient(app); _post(ann, "/enter", csrf, {"code": "p", "name": "Ann"})
    for t in titles:
        _post(ann, "/propose", csrf, {"title": t})
    with sf() as s:
        ids = {p.title: p.id for p in s.scalars(select(Proposal))}
    return sf, rid, rooms, ts[0], ids


def test_pin_route_and_generate_keeps_pin(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    sf, rid, rooms, ts, ids = _hosted_sessions(app, csrf, admin, ["A1"])
    g = TestClient(app); _post(g, "/enter", csrf, {"code": "p", "name": "G"})
    _post(g, "/rate", csrf, {"proposal_id": ids["A1"], "stars": 5})
    _post(admin, "/admin/force-place", csrf, {"round_id": rid, "timeslot_id": ts,
                                              "room_id": rooms[1], "proposal_id": ids["A1"]})

    def cell():
        with sf() as s:
            pl = s.scalar(select(Placement))
            return pl.id, pl.room_id, pl.pinned
    plid = cell()[0]
    _post(admin, "/admin/pin", csrf, {"round_id": rid, "placement_id": plid, "pinned": 0})
    assert cell()[1:] == (rooms[1], False)
    _post(admin, "/admin/pin", csrf, {"round_id": rid, "placement_id": plid, "pinned": 1})
    assert _post(admin, "/admin/solve", csrf, {"round_id": rid}).status_code == 200
    assert cell()[1:] == (rooms[1], True)


def test_solve_with_no_solution_is_409_and_keeps_grid(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    sf, rid, rooms, ts, ids = _hosted_sessions(app, csrf, admin, ["A1", "A2"])
    with sf() as s:   # Ann hosts both, pinned in one slot: no schedule fits
        for room, t in zip(rooms, ["A1", "A2"]):
            s.add(Placement(timeslot_id=ts, room_id=room, proposal_id=ids[t], pinned=True))
        s.commit()
    assert _post(admin, "/admin/solve", csrf, {"round_id": rid}).status_code == 409
    with sf() as s:
        assert len(s.scalars(select(Placement)).all()) == 2


def test_unpublish_route_and_refusal_after_clock_starts(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    sf, rid, _, ts = _two_room_round(app, csrf, admin)

    def status():
        with sf() as s:
            return s.get(Round, rid).status
    _post(admin, "/admin/publish", csrf, {"round_id": rid})
    _post(admin, "/admin/unpublish", csrf, {"round_id": rid})
    assert status() == "collecting"
    _post(admin, "/admin/publish", csrf, {"round_id": rid})
    _post(admin, "/admin/timeslot-start", csrf, {"round_id": rid, "timeslot_id": ts[0]})
    assert _post(admin, "/admin/unpublish", csrf, {"round_id": rid}).status_code == 400
    assert status() == "published"


def test_polled_grid_partial_renders(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    _, rid, _, _ = _two_room_round(app, csrf, admin)
    r = admin.get(f"/admin/grid?round_id={rid}")
    assert r.status_code == 200 and "Lib" in r.text and "Den" in r.text


def test_bootstrap_code_gates_first_event(tmp_path):
    app = create_app(Settings(secret_key="test-secret", db_path=str(tmp_path / "b.db"),
                              backup_dir=str(tmp_path / "bk"), backup_interval_s=9999,
                              bootstrap_admin_code="boot"))
    csrf = _csrf(TestClient(app)); c = TestClient(app)
    ev = {"name": "R", "participant_code": "p", "admin_code": "a"}
    assert _post(c, "/events", csrf, {**ev, "bootstrap_code": "x"}).status_code == 403
    same = {**ev, "admin_code": "p", "bootstrap_code": "boot"}
    assert _post(c, "/events", csrf, same).status_code == 400
    # a failed create must not use up the bootstrap code
    assert _post(c, "/events", csrf, ev).status_code == 403
    assert _post(c, "/events", csrf, {**ev, "bootstrap_code": "boot"}).url.path == "/admin"


def test_back_to_back_rounds_run_one_at_a_time_and_stale_pages_move_on(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    sf, r1, _, _ = _two_room_round(app, csrf, admin)
    _post(admin, "/admin/round", csrf, {"name": "R2", "min_interest": 1})
    with sf() as s:
        r2 = s.scalar(select(Round).where(Round.name == "R2")).id
    _post(admin, "/admin/timeslot", csrf, {"round_id": r2, "name": "15:00", "duration_minutes": "45"})

    # the console opens on R1. R2 waits and cannot be published first
    assert "Waiting for R1" in admin.get("/admin").text
    assert _post(admin, "/admin/publish", csrf, {"round_id": r2}).status_code == 400
    assert 'title="Archive “R1” first"' in admin.get(f"/admin?round_id={r2}").text

    sam = TestClient(app); _post(sam, "/enter", csrf, {"code": "p", "name": "Sam"})
    _post(sam, "/propose", csrf, {"title": "First"})
    with sf() as s:
        first = s.scalar(select(Proposal).where(Proposal.title == "First"))
        assert first.round_id == r1
    rate_page = f"/app/live?k={r1}:collecting"
    assert f'hx-get="/app/live?k={r1}%3Acollecting"' in sam.get("/app").text
    assert sam.get(rate_page).status_code == 204

    _post(admin, "/admin/publish", csrf, {"round_id": r1})

    # Sam's Rate page is out of date: the poll and a vote both send it to the seat
    assert sam.get(rate_page).headers.get("HX-Redirect") == "/published"
    r = _post(sam, "/rate", csrf, {"proposal_id": first.id, "stars": 2})
    assert r.headers.get("HX-Redirect") == "/published"
    with sf() as s:   # the vote did not change the published round
        assert s.scalar(select(Rating).where(Rating.proposal_id == first.id)) is None

    # during R1, participants see nothing of R2
    page = sam.get("/app").text
    assert "<b>R1</b> is published" in page and "R2" not in page and 'name="title"' not in page
    assert "R2" not in sam.get("/published").text
    seat_page = f"/app/live?k={r1}:published"
    assert sam.get(seat_page).status_code == 204

    # archive R1: the console moves on to R2, and the seat page moves on to rating
    assert "Collecting ratings" in _post(admin, "/admin/round-archive", csrf, {"round_id": r1}).text
    assert sam.get(seat_page).headers.get("HX-Redirect") == "/app"
    assert "<h1>R2</h1>" in sam.get("/app").text


def test_archive_and_restore_routes(app):
    csrf = _csrf(TestClient(app)); admin = TestClient(app)
    sf, r1, _, _ = _two_room_round(app, csrf, admin)
    _post(admin, "/admin/round", csrf, {"name": "R2", "min_interest": 1})
    # the button is in the bar, and also on a round with no timeslot yet
    for rid in (r1, app_round_id(sf)):   # R1 has a timeslot, R2 (newest) has none
        page = admin.get(f"/admin?round_id={rid}").text
        form = re.search(r'<form[^>]*action="/admin/round-archive".*?</form>', page, re.S).group(0)
        assert re.search(r'name="csrf_token" value="[^"]+"', form) and f'value="{rid}"' in form
    _post(admin, "/admin/round-archive", csrf, {"round_id": r1})
    sam = TestClient(app); _post(sam, "/enter", csrf, {"code": "p", "name": "Sam"})
    assert "<h1>R2</h1>" in sam.get("/app").text          # R1 no longer blocks R2
    assert "Archived (1)" in admin.get("/admin").text
    _post(admin, "/admin/round-restore", csrf, {"round_id": r1})
    assert "<h1>R1</h1>" in sam.get("/app").text          # restore makes R1 live again

