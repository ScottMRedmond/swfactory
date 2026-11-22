"""The core loop over the `runs` queue.

Each `tick` first asks the runner for the state of every `dispatched` row and
collects the finished ones, however late, then dispatches the oldest `queued`
rows while fewer than `limit` are in flight.
"""

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from factory.contract import ContractError, Task, parse_result
from factory.runner import Runner, RunFiles, RunState

log = logging.getLogger(__name__)

# Token-like patterns. Agent prose holding one makes the result invalid, so
# nothing is posted (software-factory.md, "Secrets in prose").
SECRET_PATTERNS = {
    "Claude token": re.compile(r"sk-ant-"),
    "GitHub token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def find_secret(value: Any) -> str | None:
    """The kind of the first token-like string anywhere in `value`, or None."""
    if isinstance(value, str):
        return next((kind for kind, p in SECRET_PATTERNS.items() if p.search(value)), None)
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return next((s for s in map(find_secret, value) if s is not None), None)
    return None


# How long past its task's timeout a still-running run is left before it's
# cancelled, for queueing and upload (orchestrator.md, "Dispatching and collecting").
TIMEOUT_GRACE = timedelta(minutes=15)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def enqueue(
    conn: sqlite3.Connection,
    *,
    repo: str,
    task: Task,
    dispatch: dict[str, Any],
    ticket: str | None = None,
    workflow_id: int | None = None,
    attempt: int = 1,
    retry_of: int | None = None,
    triggered_by: str | None = None,
) -> int:
    """Queue a run. `dispatch` is the dispatch minus `run_id`, which is the row's id."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        run_id = conn.execute(
            """INSERT INTO runs (repo, task, ticket, workflow_id, attempt, retry_of,
                                 triggered_by, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
            (repo, task, ticket, workflow_id, attempt, retry_of, triggered_by, _now()),
        ).lastrowid
        conn.execute(
            "UPDATE runs SET dispatch = ? WHERE id = ?",
            (json.dumps({**dispatch, "run_id": run_id}), run_id),
        )
    return run_id


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def tick(
    conn: sqlite3.Connection, runner: Runner, *, limit: int, now: datetime | None = None
) -> None:
    cancel_requested(conn, runner)
    collect(conn, runner, now=now)
    dispatch(conn, runner, limit=limit)


def cancel_requested(conn: sqlite3.Connection, runner: Runner) -> None:
    """Act on `factory cancel`: cancel each active row a human asked to cancel."""
    rows = conn.execute(
        """SELECT * FROM runs WHERE cancel_requested_at IS NOT NULL
           AND status IN ('queued', 'dispatched') ORDER BY id"""
    ).fetchall()
    for row in rows:
        log.info("run %s: cancelling, as asked by factory cancel", row["id"])
        cancel(conn, runner, row)


def collect(conn: sqlite3.Connection, runner: Runner, *, now: datetime | None = None) -> None:
    """Collect finished runs, and cancel ones still running well past their timeout."""
    now = now or datetime.now(UTC)
    rows = conn.execute("SELECT * FROM runs WHERE status = 'dispatched' ORDER BY id").fetchall()
    for row in rows:
        state = runner.state(row["repo"], row["runner_run_id"])
        if state is RunState.RUNNING:
            if _overdue(row, now):
                log.warning("run %s is still running past its timeout; cancelling it", row["id"])
                cancel(conn, runner, row, outcome="timed_out")
            continue
        if state is RunState.MISSING:
            log.warning("run %s is missing on the runner", row["id"])
            _finish(conn, row["id"], "failed", None)
            continue
        files = runner.files(row["repo"], row["runner_run_id"])
        outcome, result = _judge(row, files)
        _finish(conn, row["id"], outcome, result)


def _overdue(row: sqlite3.Row, now: datetime) -> bool:
    timeout = json.loads(row["dispatch"]).get("timeout_minutes")
    if timeout is None or row["dispatched_at"] is None:
        return False
    deadline = datetime.fromisoformat(row["dispatched_at"]) + timedelta(minutes=timeout)
    return now > deadline + TIMEOUT_GRACE


def cancel(
    conn: sqlite3.Connection, runner: Runner, row: sqlite3.Row, *, outcome: str = "cancelled"
) -> None:
    """End an active row: cancel it on the runner if dispatched, and mark it done
    with `outcome` and no result. Whatever the run leaves behind is discarded."""
    if row["status"] == "dispatched":
        runner.cancel(row["repo"], row["runner_run_id"])
    with conn:
        conn.execute(
            """UPDATE runs SET status = 'done', outcome = ?, result = NULL, finished_at = ?
               WHERE id = ? AND status IN ('queued', 'dispatched')""",
            (outcome, _now(), row["id"]),
        )


def _judge(row: sqlite3.Row, files: RunFiles | None) -> tuple[str, str | None]:
    """The outcome of a finished run, and the result.json to keep (as JSON text)."""
    if files is None or files.result_json is None:
        log.warning("run %s finished without a result.json", row["id"])
        return "failed", None
    try:
        data = json.loads(files.result_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning("run %s: result.json isn't JSON: %s", row["id"], e)
        return "invalid", None
    kept = json.dumps(data)
    try:
        if not isinstance(data, dict):
            raise ContractError("result.json isn't an object")
        result = parse_result(data, task=row["task"])
        if result.run_id != row["id"]:
            raise ContractError(f"result.json is for run {result.run_id}")
        if (secret := find_secret(data.get("output"))) is not None:
            raise ContractError(f"output holds something that looks like a {secret}")
    except ContractError as e:
        log.warning("run %s: invalid result.json: %s", row["id"], e)
        return "invalid", kept
    return result.outcome, kept


def _finish(conn: sqlite3.Connection, run_id: int, outcome: str, result: str | None) -> None:
    with conn:
        conn.execute(
            """UPDATE runs SET status = 'done', outcome = ?, result = ?, finished_at = ?
               WHERE id = ? AND status = 'dispatched'""",
            (outcome, result, _now(), run_id),
        )


def dispatch(conn: sqlite3.Connection, runner: Runner, *, limit: int) -> None:
    while True:
        in_flight = conn.execute("SELECT count(*) FROM runs WHERE status = 'dispatched'").fetchone()[0]
        if in_flight >= limit:
            return
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return
        runner_run_id = runner.dispatch(row["repo"], json.loads(row["dispatch"]))
        with conn:
            conn.execute(
                """UPDATE runs SET status = 'dispatched', runner_run_id = ?, dispatched_at = ?
                   WHERE id = ?""",
                (runner_run_id, _now(), row["id"]),
            )
