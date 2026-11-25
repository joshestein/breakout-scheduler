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
        assert not {"anonymous", "must_run", "allow_repeat", "needs_space", "merged_into"} & cols


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
        assert set(mig.get_table_names()) - {"alembic_version"} == set(new.get_table_names())
        for table in new.get_table_names():
            assert ({c["name"] for c in mig.get_columns(table)}
                    == {c["name"] for c in new.get_columns(table)}), table


def test_upgrade_moves_archived_at_into_status(tmp_path):
    """archived_at becomes status 'archived'. Participants now see the oldest round that
    is not archived, so the upgrade also archives rounds older than an event's newest
    published round: each event keeps showing the schedule it showed before."""
    db = str(tmp_path / "old.db")
    cfg = _alembic_cfg(db)
    command.upgrade(cfg, "d4e8a1c6b2f7")
    with create_engine(f"sqlite:///{db}").begin() as c:
        c.exec_driver_sql("insert into events (id, name, participant_code, admin_code, created_at) "
                          "values (1, 'A', 'a', 'aa', '2026-01-01'), (2, 'B', 'b', 'bb', '2026-01-01')")
        c.exec_driver_sql(
            "insert into rounds (id, event_id, name, status, min_interest, created_at) values "
            "(1, 1, 'old', 'published', 2, '2026-01-01'), (2, 1, 'abandoned', 'collecting', 2, '2026-01-01'), "
            "(3, 1, 'newest', 'published', 2, '2026-01-01'), (4, 1, 'next', 'collecting', 2, '2026-01-01'), "
            "(5, 2, 'only', 'collecting', 2, '2026-01-01')")
        c.exec_driver_sql(
            "insert into rounds (id, event_id, name, status, min_interest, created_at, archived_at) "
            "values (6, 2, 'dropped', 'collecting', 2, '2026-01-01', '2026-01-02')")
    command.upgrade(cfg, "head")
    with create_engine(f"sqlite:///{db}").connect() as c:
        status = dict(c.exec_driver_sql("select id, status from rounds").fetchall())
    assert status == {1: "archived", 2: "archived", 3: "published", 4: "collecting",
                      5: "collecting", 6: "archived"}


def test_fresh_file_db_is_stamped_at_head(tmp_path):
    """create_all on a brand-new DB must record the Alembic head, so a later
    RUN_MIGRATIONS=1 upgrade does not try to re-create existing tables."""
    from alembic.script import ScriptDirectory
    from app.config import Settings
    from app.webapp import create_app

    db = str(tmp_path / "fresh.db")
    create_app(Settings(secret_key="k", db_path=db, backup_dir=str(tmp_path),
                        backup_interval_s=9999,
                        bootstrap_admin_code=None))
    head = ScriptDirectory.from_config(_alembic_cfg(db)).get_current_head()
    with create_engine(f"sqlite:///{db}").connect() as c:
        assert c.exec_driver_sql("select version_num from alembic_version").scalar() == head

