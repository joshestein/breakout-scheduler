from types import SimpleNamespace

from fastapi import FastAPI, Request

from app.auth import issue_token, read_token, SessionToken
from app.csrf import new_token, verify
from app.ratelimit import Limiter, client_ip


def _req(peer: str, headers: dict[str, str], trusted: frozenset[str] = frozenset({"10.0.0.2"})):
    app = FastAPI()
    app.state.settings = SimpleNamespace(trusted_proxies=trusted)
    return Request({"type": "http", "app": app, "client": (peer, 0),
                    "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})


def test_token_roundtrip():
    t = issue_token(42, "participant", "secret")
    assert read_token(t, "secret") == SessionToken(pid=42, scope="participant")


def test_token_wrong_secret_rejected():
    t = issue_token(42, "admin", "secret")
    assert read_token(t, "other") is None


def test_admin_scope_cannot_be_forged_from_participant_token():
    t = issue_token(7, "participant", "secret")
    tok = read_token(t, "secret")
    assert tok is not None and not tok.is_admin
    # tampering the payload breaks the signature
    assert read_token(t[:-3] + "abc", "secret") is None


def test_csrf_roundtrip_and_reject():
    t = new_token("secret")
    assert verify(t, "secret") is True
    assert verify(t, "other") is False
    assert verify(None, "secret") is False


def test_ratelimit_backoff_and_reset():
    lim = Limiter(base_s=1.0, cap_s=60.0)
    assert lim.allowed("ip1", now=0.0)
    lim.record_failure("ip1", now=0.0)        # blocked until 1.0
    assert not lim.allowed("ip1", now=0.5)
    assert lim.allowed("ip1", now=1.0)
    lim.record_failure("ip1", now=1.0)        # 2nd failure -> 2s
    assert not lim.allowed("ip1", now=2.5)
    lim.reset("ip1")
    assert lim.allowed("ip1", now=2.5)


def test_client_ip_ignores_header_from_untrusted_peer():
    r = _req("203.0.113.9", {"cf-connecting-ip": "1.1.1.1"})
    assert client_ip(r) == "203.0.113.9"


def test_client_ip_uses_header_from_trusted_peer():
    r = _req("10.0.0.2", {"cf-connecting-ip": "1.1.1.1"})
    assert client_ip(r) == "1.1.1.1"


def test_client_ip_falls_back_to_peer_without_header():
    assert client_ip(_req("10.0.0.2", {})) == "10.0.0.2"
