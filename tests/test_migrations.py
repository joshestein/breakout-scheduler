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


def test_upgrade_folds_merged_proposals_into_their_root():
    """merged_into is dropped; the merged proposal's ratings and hosts must land on
    its root first (max stars per person), as the old code did at read time."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "m.db")
        cfg = _alembic_cfg(db)
        command.upgrade(cfg, "5433f972a0e2")
        eng = create_engine(f"sqlite:///{db}")
        with eng.begin() as c:
            x = c.exec_driver_sql
            x("INSERT INTO events (id, name, participant_code, admin_code, descriptions_enabled, created_at) "
              "VALUES (1, 'E', 'p', 'a', 1, CURRENT_TIMESTAMP)")
            for pid, name in [(1, "Ann"), (2, "Bob"), (3, "Cy")]:
                x(f"INSERT INTO participants (id, event_id, display_name, is_admin, created_at) "
                  f"VALUES ({pid}, 1, '{name}', 0, CURRENT_TIMESTAMP)")
            x("INSERT INTO proposals (id, event_id, title, status, created_by, created_at) "
              "VALUES (10, 1, 'Root', 'active', 1, CURRENT_TIMESTAMP)")
            x("INSERT INTO proposals (id, event_id, title, status, created_by, merged_into, created_at) "
              "VALUES (11, 1, 'Dup', 'retired', 2, 10, CURRENT_TIMESTAMP)")
            x("INSERT INTO ratings (participant_id, proposal_id, stars, updated_at) VALUES "
              "(3, 10, 3, CURRENT_TIMESTAMP), (3, 11, 5, CURRENT_TIMESTAMP), (1, 11, 4, CURRENT_TIMESTAMP)")
            x("INSERT INTO proposal_runners (proposal_id, participant_id) VALUES (11, 2)")
            # an old retire/merge left the merged proposal's placement behind
            x("INSERT INTO rooms (id, event_id, name, capacity) VALUES (1, 1, 'Lib', 10)")
            x("INSERT INTO rounds (id, event_id, name, status, lambda_fairness, min_interest, created_at) "
              "VALUES (1, 1, 'R', 'published', 3, 1, CURRENT_TIMESTAMP)")
            x("INSERT INTO timeslots (id, round_id, name, position, duration_minutes) "
              "VALUES (1, 1, '14:00', 0, 45)")
            x("INSERT INTO placements (timeslot_id, room_id, proposal_id, pinned) VALUES (1, 1, 11, 0)")
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            stars = dict(c.exec_driver_sql(
                "SELECT participant_id, stars FROM ratings WHERE proposal_id = 10").all())
            hosts = {r[0] for r in c.exec_driver_sql(
                "SELECT participant_id FROM proposal_runners WHERE proposal_id = 10")}
        with eng.connect() as c:
            left = c.exec_driver_sql("SELECT count(*) FROM placements").scalar()
        assert stars == {3: 5, 1: 4}
        assert hosts == {2}
        assert left == 0

