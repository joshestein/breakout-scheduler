#!/usr/bin/env bash
# Redeploy on the VPS. CI runs this over SSH after it fast-forwards the checkout.
# Steps: back up the DB, rebuild, start with migrations on, wait for healthy.
# Set COMPOSE_PROFILES=caddy (or tunnel) in .env so the proxy is kept up too.
set -euo pipefail

cd "$(dirname "$0")/.."

stamp="$(date -u +%Y%m%d-%H%M%S)"
keep=10

echo "==> Pre-deploy backup (predeploy-${stamp}.db)"
# `run` (not `exec`) so the backup works even when the app container is down.
# It also refuses to migrate a DB that has tables but no Alembic version:
# `upgrade head` would try to create existing tables and crash-loop the app.
docker compose run --rm --no-deps -T app python - "$stamp" "$keep" <<'PY'
import glob, os, sqlite3, sys

stamp, keep = sys.argv[1], int(sys.argv[2])
db, backup_dir = "/data/app.db", "/data/backups"
if not os.path.exists(db):
    print("No DB yet: skipping backup (first deploy).")
    sys.exit(0)

con = sqlite3.connect(db)
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if tables and "alembic_version" not in tables:
    sys.exit("DB has tables but no alembic_version. Stamp it before migrating: "
             "docker compose run --rm app alembic stamp <revision>")

os.makedirs(backup_dir, exist_ok=True)
dest = os.path.join(backup_dir, f"predeploy-{stamp}.db")
con.execute("VACUUM INTO ?", (dest,))
con.close()
print(f"Backup written: {dest}")

for old in sorted(glob.glob(os.path.join(backup_dir, "predeploy-*.db")))[:-keep]:
    os.remove(old)
PY

echo "==> Build, start (RUN_MIGRATIONS=1), wait for healthy"
# Shell env overrides .env, so this enables migrations for this start only.
if ! RUN_MIGRATIONS=1 docker compose up -d --build --wait --wait-timeout 90; then
  docker compose logs --tail=100 app >&2
  echo "Restore if needed: copy /data/backups/predeploy-${stamp}.db over /data/app.db, check out the previous commit, rerun." >&2
  exit 1
fi
docker image prune -f >/dev/null
