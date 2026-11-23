import base64
import json
import os
from dataclasses import dataclass


def _resolve_vapid(db_path: str) -> tuple[str, str]:
    """Return (public_b64url, private_pem). Precedence: explicit env vars win;
    otherwise load a persisted keypair next to the DB; otherwise generate one and
    save it there so push subscriptions survive restarts (rotating keys would
    silently invalidate every existing subscription)."""
    env_pub = os.environ.get("VAPID_PUBLIC_KEY", "")
    env_priv = os.environ.get("VAPID_PRIVATE_KEY", "")
    if env_pub and env_priv:
        return env_pub, env_priv
    # don't persist for in-memory/test DBs, and only auto-generate when asked to
    if db_path == ":memory:" or os.environ.get("VAPID_AUTOGEN", "1") != "1":
        return env_pub, env_priv
    path = os.path.join(os.path.dirname(db_path) or ".", "vapid.json")
    try:
        if os.path.exists(path):
            data = json.load(open(path))
            return data["public"], data["private"]
    except Exception:
        pass  # fall through and regenerate on a corrupt/unreadable file
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        priv = ec.generate_private_key(ec.SECP256R1())
        priv_pem = priv.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        pub_pt = priv.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        pub_b64 = base64.urlsafe_b64encode(pub_pt).rstrip(b"=").decode()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"public": pub_b64, "private": priv_pem}, f)
        os.chmod(path, 0o600)
        return pub_b64, priv_pem
    except Exception:
        return env_pub, env_priv  # cryptography missing/unwritable → push stays off


@dataclass(frozen=True)
class Settings:
    secret_key: str
    db_path: str
    backup_dir: str
    backup_interval_s: int
    bootstrap_admin_code: str | None
    cookie_secure: bool = False
    base_url: str = ""  # canonical public origin for share links (else derived from request)
    vapid_public_key: str = ""    # browser applicationServerKey (base64url)
    vapid_private_key: str = ""   # PEM, server-only (Web Push signing)
    vapid_subject: str = "mailto:admin@example.com"
    alert_lead_minutes: int = 5   # push a heads-up this many minutes before a slot ends
    # peers whose CF-Connecting-IP we trust (the tunnel connects from loopback)
    trusted_proxies: frozenset[str] = frozenset({"127.0.0.1", "::1"})

    @property
    def push_enabled(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)


def load_settings() -> Settings:
    db_path = os.environ.get("DB_PATH", "data/app.db")
    vapid_public, vapid_private = _resolve_vapid(db_path)
    return Settings(
        secret_key=os.environ.get("SECRET_KEY", "dev-insecure-key-change-me"),
        db_path=db_path,
        backup_dir=os.environ.get("BACKUP_DIR", "data/backups"),
        backup_interval_s=int(os.environ.get("BACKUP_INTERVAL_S", "300")),
        bootstrap_admin_code=os.environ.get("BOOTSTRAP_ADMIN_CODE") or None,
        # Behind TLS-terminating proxies (Cloudflare Tunnel) the app sees http,
        # so set COOKIE_SECURE=1 in production to force Secure cookies.
        cookie_secure=os.environ.get("COOKIE_SECURE", "0") == "1",
        base_url=os.environ.get("BASE_URL", ""),
        vapid_public_key=vapid_public,
        vapid_private_key=vapid_private,
        vapid_subject=os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com"),
        trusted_proxies=frozenset({"127.0.0.1", "::1"}
                                  | {p.strip() for p in os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()}),
    )
