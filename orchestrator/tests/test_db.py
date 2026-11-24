"""The orchestrator's SQLite file: migrations, backups before migrating, WAL."""

import sqlite3

from factory import db

RUNS_COLUMNS = [
    "id", "repo", "task", "ticket", "workflow_id", "attempt", "retry_of",
    "triggered_by", "status", "outcome", "runner_run_id", "dispatch", "result",
    "created_at", "dispatched_at", "finished_at",
    "handled_at",  # not in the design doc: when the orchestrator acted on the result
    "log_url",  # not in the design doc: the runner's log link, for the CLI
    "cancel_requested_at",  # not in the design doc: when `factory cancel` asked
    "triggered_by_id",  # not in the design doc: triggered_by's numeric GitHub user id
    "shown_at",  # not in the design doc: when a done run's line was shown in its status comment
    "validation_error",  # not in the design doc: why an invalid result was invalid
]


def columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_a_new_database_has_the_runs_table_from_the_design(tmp_path):
    conn = db.connect(tmp_path / "factory.db")
    assert columns(conn, "runs") == RUNS_COLUMNS


def test_migrations_are_tracked_with_user_version(tmp_path):
    conn = db.connect(tmp_path / "factory.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_reconnecting_runs_no_migration_and_keeps_the_rows(tmp_path):
    path = tmp_path / "factory.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO runs (repo, task, status, created_at) VALUES ('o/r', 'smoke', 'queued', 'now')")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert list(tmp_path.glob("*.before-v*")) == []


def test_runs_in_wal_mode(tmp_path):
    conn = db.connect(tmp_path / "factory.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_an_existing_database_is_copied_before_migrating(tmp_path, monkeypatch):
    path = tmp_path / "factory.db"
    db.connect(path).close()
    version = len(db.MIGRATIONS)

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, "CREATE TABLE extra (x)"])
    db.connect(path).close()

    backup = tmp_path / f"factory.db.before-v{version + 1}"
    old = sqlite3.connect(backup)
    assert old.execute("PRAGMA user_version").fetchone()[0] == version
    assert "extra" not in [r[0] for r in old.execute("SELECT name FROM sqlite_master")]


def test_a_failed_migration_leaves_the_database_as_it_was(tmp_path, monkeypatch):
    path = tmp_path / "factory.db"
    db.connect(path).close()
    version = len(db.MIGRATIONS)

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, "CREATE TABLE extra (x); NOT SQL"])
    try:
        db.connect(path)
    except sqlite3.Error:
        pass
    else:
        raise AssertionError("a bad migration should raise")

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    assert "extra" not in [r[0] for r in conn.execute("SELECT name FROM sqlite_master")]
