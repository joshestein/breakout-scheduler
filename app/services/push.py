"""Web Push: subscribe + send. Endpoints are client-supplied, so we restrict
delivery to known push services (SSRF guard) and run sends off the request path."""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PushSubscription, Participant, Round

log = logging.getLogger("breakouts")

# Only real push services — never let a client point delivery at internal hosts.
_ALLOWED_SUFFIXES = (
    "fcm.googleapis.com",            # Chrome / Android
    "push.services.mozilla.com",     # Firefox
    "notify.windows.com",            # Edge / Windows
    "push.apple.com",                # Safari (incl. web.push.apple.com)
)


def allowed_endpoint(url: str) -> bool:
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname.lower()
    return any(host == s or host.endswith("." + s) for s in _ALLOWED_SUFFIXES)


def subscribe(session: Session, *, participant_id: int, sub: dict) -> None:
    endpoint = (sub or {}).get("endpoint", "")
    keys = (sub or {}).get("keys", {}) or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (endpoint and p256dh and auth):
        raise ValueError("invalid subscription")
    if not allowed_endpoint(endpoint):
        raise ValueError("unsupported push endpoint")
    existing = session.scalar(select(PushSubscription).where(
        PushSubscription.endpoint == endpoint))
    if existing is None:
        session.add(PushSubscription(participant_id=participant_id, endpoint=endpoint,
                                     p256dh=p256dh, auth=auth))
    else:  # re-subscribe (possibly a different participant on this browser)
        existing.participant_id = participant_id
        existing.p256dh = p256dh
        existing.auth = auth
    session.flush()


def notify_round(session_factory, settings, round_id: int, title: str, body: str,
                 url: str = "/published") -> int:
    """Send a push to every subscription of participants in this round's event.
    Returns the number sent. Safe no-op when push isn't configured."""
    if not settings.push_enabled:
        return 0
    from pywebpush import webpush, WebPushException  # lazy: only when enabled
    s = session_factory()
    sent = 0
    try:
        rnd = s.get(Round, round_id)
        if rnd is None:
            return 0
        part_ids = list(s.scalars(
            select(Participant.id).where(Participant.event_id == rnd.event_id)))
        if not part_ids:
            return 0
        subs = list(s.scalars(select(PushSubscription).where(
            PushSubscription.participant_id.in_(part_ids))))
        payload = json.dumps({"title": title, "body": body, "url": url})
        for sub in subs:
            try:
                webpush(
                    subscription_info={"endpoint": sub.endpoint,
                                       "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                    data=payload,
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims={"sub": settings.vapid_subject},
                    timeout=10,
                )
                sent += 1
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (404, 410):  # gone — prune dead subscription
                    log.info("pruning dead push subscription %s (HTTP %s)", sub.endpoint, code)
                    s.delete(sub)
                else:
                    log.warning("webpush failed (HTTP %s) endpoint=%s", code, sub.endpoint)
            except Exception:
                # don't fail silently — a misconfigured VAPID key fails EVERY send,
                # and without this log the feature looks "on" while delivering nothing.
                log.exception("webpush send error endpoint=%s", sub.endpoint)
        if subs and sent == 0:
            log.error("push: 0 of %d sends succeeded — likely a VAPID/config problem",
                      len(subs))
        s.commit()
    finally:
        s.close()
    return sent
