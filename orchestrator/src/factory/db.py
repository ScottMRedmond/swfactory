"""The orchestrator's one SQLite file.

Migrations are a numbered list of SQL steps: step N takes the database from
`PRAGMA user_version` N-1 to N. Before migrating an existing database the file
is copied to `<path>.before-v<N>`, so a bad migration is a one-command rollback.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: the runs queue. One row is one dispatch; a retry is a new row.
    """
    CREATE TABLE runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        repo          TEXT NOT NULL,
        task          TEXT NOT NULL,
        ticket        TEXT,
        workflow_id   INTEGER,
        attempt       INTEGER NOT NULL DEFAULT 1,
        retry_of      INTEGER REFERENCES runs(id),
        triggered_by  TEXT,
        status        TEXT NOT NULL CHECK (status IN ('queued', 'dispatched', 'done')),
        outcome       TEXT CHECK (outcome IN
                        ('succeeded', 'failed', 'invalid', 'timed_out', 'cancelled', 'superseded')),
        runner_run_id TEXT,
        dispatch      TEXT,
        result        TEXT,
        created_at    TEXT NOT NULL,
        dispatched_at TEXT,
        finished_at   TEXT
    );
    CREATE INDEX runs_status ON runs (status, id);
    """,
    # 2: when the orchestrator finished acting on a done run's result (e.g. posted
    # explore's findings). NULL on a done row means there's still work to do.
    """
    ALTER TABLE runs ADD COLUMN handled_at TEXT;
    """,
    # 3: what the operator CLI shows besides runs (#35). See factory.state.
    """
    ALTER TABLE runs ADD COLUMN log_url TEXT;
    CREATE TABLE settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE usage_readings (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        read_at TEXT NOT NULL,
        limits  TEXT,
        error   TEXT
    );
    CREATE TABLE polls (
        repo            TEXT PRIMARY KEY,
        last_success_at TEXT NOT NULL
    );
    CREATE TABLE problems (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT NOT NULL,
        repo       TEXT,
        summary    TEXT NOT NULL,
        issue      TEXT,
        opened_at  TEXT NOT NULL,
        closed_at  TEXT
    );
    CREATE UNIQUE INDEX problems_open ON problems (kind, coalesce(repo, '')) WHERE closed_at IS NULL;
    """,
    # 4: when `factory cancel` asked for the run to be cancelled. The orchestrator
    # acts on it in its next loop, if the run is still queued or dispatched.
    """
    ALTER TABLE runs ADD COLUMN cancel_requested_at TEXT;
    """,
    # 5: enrolments, as last read from GitHub (one row per repo the App is or was
    # installed on), and workflows, which unenrolling stops.
    """
    CREATE TABLE repos (
        repo          TEXT PRIMARY KEY,
        state         TEXT NOT NULL CHECK (state IN
                        ('enrolled', 'not_enrolled', 'invalid', 'uninstalled')),
        enrolment_sha TEXT,
        config        TEXT,
        error         TEXT,
        labels_at     TEXT,
        checked_at    TEXT NOT NULL
    );
    CREATE TABLE workflows (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        repo      TEXT NOT NULL,
        kind      TEXT NOT NULL,
        subject   TEXT NOT NULL,
        branch    TEXT,
        pr        INTEGER,
        status    TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'done', 'stopped', 'cancelled')),
        spec_hash TEXT
    );
    CREATE INDEX workflows_repo ON workflows (repo, status);
    """,
    # 6: the numeric GitHub user id of `triggered_by` (implement and review
    # dispatches carry `triggered_by: {login, id}` for the commit trailer).
    """
    ALTER TABLE runs ADD COLUMN triggered_by_id INTEGER;
    """,
    # 7: each issue or PR's status comment (orchestrator.md#observability), and
    # when a done run's final line was shown on it. Runs already done count as
    # shown, so no comment is made for old work.
    """
    CREATE TABLE status_comments (
        repo       TEXT NOT NULL,
        number     INTEGER NOT NULL,
        comment_id INTEGER NOT NULL,
        body       TEXT NOT NULL,
        PRIMARY KEY (repo, number)
    );
    ALTER TABLE runs ADD COLUMN shown_at TEXT;
    UPDATE runs SET shown_at = finished_at WHERE status = 'done';
    """,
]


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database in WAL mode and bring it up to date."""
    path = Path(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        _migrate(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open the database for reading only (the CLI). Never creates or migrates it."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect_readwrite(path: str | Path) -> sqlite3.Connection:
    """Open an existing database to write to (the CLI's requests). Never creates
    or migrates it; check `schema_current` before writing."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=rw", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def schema_current(conn: sqlite3.Connection) -> bool:
    return _version(conn) == len(MIGRATIONS)


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    current = _version(conn)
    target = len(MIGRATIONS)
    if current >= target:
        return
    if current > 0:
        with closing(sqlite3.connect(path.with_name(f"{path.name}.before-v{target}"))) as backup:
            conn.backup(backup)
    for version in range(current + 1, target + 1):
        try:
            conn.executescript(
                f"BEGIN;\n{MIGRATIONS[version - 1]};\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
