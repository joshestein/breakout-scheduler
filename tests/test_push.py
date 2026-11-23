import pytest

from app.services import push as PUSH
from app.config import Settings
from app.models import Event, Participant, PushSubscription


def test_allowed_endpoint_ssrf_guard():
    assert PUSH.allowed_endpoint("https://fcm.googleapis.com/fcm/send/x")
    assert PUSH.allowed_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert not PUSH.allowed_endpoint("http://fcm.googleapis.com/x")        # not https
    assert not PUSH.allowed_endpoint("https://169.254.169.254/latest/meta")  # internal
    assert not PUSH.allowed_endpoint("https://attacker.example/x")          # not a push host


def _evt(s):
    e = Event(name="R", participant_code="p", admin_code="a"); s.add(e); s.flush()
    u = Participant(event_id=e.id, display_name="Sam"); s.add(u); s.flush()
    return u


def test_subscribe_validates_and_upserts(session):
    u = _evt(session)
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "a", "auth": "b"}}
    with pytest.raises(ValueError):  # SSRF endpoint rejected
        PUSH.subscribe(session, participant_id=u.id,
                       sub={"endpoint": "https://evil.example/x", "keys": {"p256dh": "a", "auth": "b"}})
    with pytest.raises(ValueError):  # missing keys
        PUSH.subscribe(session, participant_id=u.id, sub={"endpoint": good["endpoint"]})
    PUSH.subscribe(session, participant_id=u.id, sub=good)
    assert session.query(PushSubscription).count() == 1
    PUSH.subscribe(session, participant_id=u.id, sub=good)   # same endpoint -> upsert
    assert session.query(PushSubscription).count() == 1


def test_notify_round_noop_when_push_disabled():
    s = Settings(secret_key="k", db_path=":memory:", backup_dir="/tmp", backup_interval_s=9,
                 bootstrap_admin_code=None)
    assert s.push_enabled is False
    assert PUSH.notify_round(lambda: None, s, 1, "t", "b") == 0   # returns before touching DB
