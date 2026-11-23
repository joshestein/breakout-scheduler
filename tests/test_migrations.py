"""Exercise the Alembic upgrade path that production uses with RUN_MIGRATIONS=1.
The test suite otherwise builds schema via create_all, so this is the only thing
guarding against migration breakage (e.g. unnamed constraints in SQLite batch mode)."""
import os
import tempfile

from sqlalchemy import create_engine, inspect
from alembic import command
from alembic.config import Config as AlembicConfig

from app.models import Base

ROOT = os.path.dirname(os.path.dirname(__file__))


def _alembic_cfg(db_path: str) -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(ROOT, "alembic.ini"))
    os.environ["DB_PATH"] = db_path
    return cfg


def test_alembic_upgrade_head_on_fresh_db():
    """`alembic upgrade head` must apply cleanly start-to-finish on an empty DB."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "fresh.db")
        command.upgrade(_alembic_cfg(db), "head")   # must not raise
        cols = {c["name"] for c in inspect(create_engine(f"sqlite:///{db}"))
                .get_columns("proposals")}
        # columns added across the migration chain are all present
        assert {"round_id", "min_interest_override", "max_attendees"} <= cols
        assert not {"anonymous", "must_run", "allow_repeat", "needs_space"} & cols


def test_migrated_schema_matches_create_all():
    """The two provisioning paths (Alembic vs create_all) must converge, so the
    fast-restart create_all path can't silently drift from migrated deploys."""
    with tempfile.TemporaryDirectory() as d:
        migrated = os.path.join(d, "migrated.db")
        command.upgrade(_alembic_cfg(migrated), "head")
        fresh = create_engine("sqlite://")
        Base.metadata.create_all(fresh)

        mig = inspect(create_engine(f"sqlite:///{migrated}"))
        new = inspect(fresh)
        for table in ("proposals", "participants", "timeslot_rooms"):
            assert ({c["name"] for c in mig.get_columns(table)}
                    == {c["name"] for c in new.get_columns(table)}), table


def test_fresh_file_db_is_stamped_at_head(tmp_path):
    """create_all on a brand-new DB must record the Alembic head, so a later
    RUN_MIGRATIONS=1 upgrade does not try to re-create existing tables."""
    from alembic.script import ScriptDirectory
    from app.config import Settings
    from app.webapp import create_app

    db = str(tmp_path / "fresh.db")
    create_app(Settings(secret_key="k", db_path=db, backup_dir=str(tmp_path),
                        backup_interval_s=9999, preview_interval_s=9999,
                        bootstrap_admin_code=None))
    head = ScriptDirectory.from_config(_alembic_cfg(db)).get_current_head()
    with create_engine(f"sqlite:///{db}").connect() as c:
        assert c.exec_driver_sql("select version_num from alembic_version").scalar() == head
