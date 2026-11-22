"""The orchestrator's state besides `runs`, as `factory status` reads it.

Later tickets fill these in: the pause flag (`factory pause`/`resume`, #19),
usage readings (#19), the last successful poll per repo (#6/#8) and open
factory problems (#20). Writers take a read-write connection; readers work on
the CLI's read-only one.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- the pause flag ---


def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
    with conn:
        conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES ('paused', ?, ?)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (json.dumps(paused), _now()),
        )


def is_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = 'paused'").fetchone()
    return bool(row and json.loads(row["value"]))


# --- the runner's availability ---
#
# Since when the runner hasn't answered (or has refused runs), and when it last
# refused one (on Actions: the spending limit), so dispatching backs off.


def _get(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else json.loads(row["value"])


def _set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                (key, json.dumps(value), _now()),
            )


def runner_unavailable_since(conn: sqlite3.Connection) -> str | None:
    return _get(conn, "runner_unavailable_since")


def set_runner_unavailable_since(conn: sqlite3.Connection, at: str | None) -> None:
    _set(conn, "runner_unavailable_since", at)


def runner_refused_at(conn: sqlite3.Connection) -> str | None:
    return _get(conn, "runner_refused_at")


def set_runner_refused_at(conn: sqlite3.Connection, at: str | None) -> None:
    _set(conn, "runner_refused_at", at)


# --- Claude usage ---


@dataclass(frozen=True)
class UsageReading:
    read_at: str
    limits: list[dict[str, Any]] | None  # usage_report.rate_limits.limits[]: kind, percent, resets_at
    error: str | None  # why it couldn't be read; then limits is None


def record_usage(
    conn: sqlite3.Connection,
    limits: list[dict[str, Any]] | None,
    *,
    error: str | None = None,
    read_at: str | None = None,
) -> None:
    """Store a usage reading: the `limits[]` rows, or None and why it couldn't be read."""
    with conn:
        conn.execute(
            "INSERT INTO usage_readings (read_at, limits, error) VALUES (?, ?, ?)",
            (read_at or _now(), None if limits is None else json.dumps(limits), error),
        )


def latest_usage(conn: sqlite3.Connection) -> UsageReading | None:
    row = conn.execute("SELECT * FROM usage_readings ORDER BY read_at DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    limits = None if row["limits"] is None else json.loads(row["limits"])
    return UsageReading(read_at=row["read_at"], limits=limits, error=row["error"])


# --- polls ---


def record_poll(conn: sqlite3.Connection, repo: str, *, at: str | None = None) -> None:
    """A poll of `repo` succeeded."""
    with conn:
        conn.execute(
            """INSERT INTO polls (repo, last_success_at) VALUES (?, ?)
               ON CONFLICT (repo) DO UPDATE SET last_success_at = excluded.last_success_at""",
            (repo, at or _now()),
        )


def last_polls(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["repo"], r["last_success_at"]) for r in conn.execute("SELECT * FROM polls ORDER BY repo")]


# --- factory problems ---


def open_problem(
    conn: sqlite3.Connection,
    kind: str,
    summary: str,
    *,
    repo: str | None = None,
    issue: str | None = None,
) -> int:
    """Open a problem, or update the open one of the same kind and repo. Returns its id.

    `repo` is the repo it's about (None: the whole factory); `issue` is the
    GitHub issue it was opened as (`owner/repo#n`), once there is one.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT id FROM problems WHERE kind = ? AND coalesce(repo, '') = coalesce(?, '')
               AND closed_at IS NULL""",
            (kind, repo),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE problems SET summary = ?, issue = coalesce(?, issue) WHERE id = ?",
                (summary, issue, row["id"]),
            )
            return row["id"]
        return conn.execute(
            "INSERT INTO problems (kind, repo, summary, issue, opened_at) VALUES (?, ?, ?, ?, ?)",
            (kind, repo, summary, issue, _now()),
        ).lastrowid


def close_problem(conn: sqlite3.Connection, kind: str, *, repo: str | None = None) -> None:
    with conn:
        conn.execute(
            """UPDATE problems SET closed_at = ?
               WHERE kind = ? AND coalesce(repo, '') = coalesce(?, '') AND closed_at IS NULL""",
            (_now(), kind, repo),
        )


def open_problems(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM problems WHERE closed_at IS NULL ORDER BY id").fetchall()
