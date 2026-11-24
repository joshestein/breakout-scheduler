from __future__ import annotations

from fastapi import Request, HTTPException, Depends

from app.auth import read_token, SessionToken
from app.csrf import verify as csrf_verify
from app.models import Participant, as_utc


def participant_cb(p: Participant) -> int:
    """Identity-binding value baked into the cookie: the participant's creation time.
    If the DB is rebuilt and the id is reused for someone else, this won't match."""
    return int(as_utc(p.created_at).timestamp())


def load_participant(session, token: SessionToken | None) -> Participant | None:
    """Resolve the cookie to a participant, rejecting a stale/colliding id.
    Legacy cookies (no cb) are grandfathered so existing sessions aren't disabled."""
    if token is None:
        return None
    p = session.get(Participant, token.pid)
    if p is None:
        return None
    if token.cb is not None and participant_cb(p) != token.cb:
        return None  # id now belongs to a different person (DB rebuilt) — don't impersonate
    return p


def get_session(request: Request):
    factory = request.app.state.session_factory
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def current_token(request: Request) -> SessionToken | None:
    secret = request.app.state.settings.secret_key
    return read_token(request.cookies.get("session"), secret)


def require_participant(token: SessionToken | None = Depends(current_token)) -> SessionToken:
    if token is None:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return token


def require_admin(token: SessionToken | None = Depends(current_token)) -> SessionToken:
    if token is None or not token.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return token


async def require_csrf(request: Request) -> None:
    secret = request.app.state.settings.secret_key
    token = request.headers.get("X-CSRFToken")
    if token is None:
        form = await request.form()
        token = form.get("csrf_token")
    if not csrf_verify(token, secret):
        raise HTTPException(status_code=400, detail="bad csrf token")
