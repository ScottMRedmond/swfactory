"""The core loop over the `runs` queue.

Each `tick` first asks the runner for the state of every `dispatched` row and
collects the finished ones, however late, then dispatches the oldest `queued`
rows while fewer than `limit` are in flight.
"""

import json
import logging
import sqlite3
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from factory import state as db_state
from factory import tokenscan
from factory.contract import ContractError, Task, parse_result
from factory.logs import about_run
from factory.runner import Runner, RunFiles, RunnerUnavailable, RunRefused, RunState

log = logging.getLogger(__name__)


def find_secret(value: Any) -> str | None:
    """The kind of the first token-like string anywhere in `value`, or None."""
    if isinstance(value, str):
        # Agent prose holding one makes the result invalid, so nothing is
        # posted (software-factory.md, "Secrets in prose").
        return next(iter(tokenscan.find(value)), None)
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return next((s for s in map(find_secret, value) if s is not None), None)
    return None


class TryLater(Exception):
    """A `check` or `prepare` hook couldn't do its work now (e.g. GitHub or git
    is unreachable): the row is left as it is, to be tried on a later tick."""


# Checks a succeeded result beyond its schema (e.g. its bundle), given the
# row, the parsed result.json and the run's files. Raises `ContractError` to
# make the result invalid, or `TryLater`.
Check = Callable[[sqlite3.Row, dict[str, Any], RunFiles], None]
# Freshens a queued row's dispatch just before it goes out (e.g. implement's
# base_sha). Returns the dispatch to send, or None to send it as queued.
Prepare = Callable[[sqlite3.Row], dict[str, Any] | None]


def _about(row: sqlite3.Row) -> dict[str, Any]:
    return about_run(id=row["id"], repo=row["repo"], ticket=row["ticket"])


# How long past its task's timeout a still-running run is left before it's
# cancelled, for queueing and upload (orchestrator.md, "Dispatching and collecting").
TIMEOUT_GRACE = timedelta(minutes=15)
# After the runner refuses a run (on Actions: the spending limit), dispatching
# waits this long, so the refused row isn't dispatched again every tick.
REFUSED_BACKOFF = timedelta(minutes=15)
# A row claimed for dispatching this long ago that never got a runner run id
# was left by a process that died mid-dispatch.
CLAIM_STALE = timedelta(minutes=10)
# A runner unavailable this long is a factory problem.
UNAVAILABLE_PROBLEM_AFTER = timedelta(minutes=30)


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
    triggered_by_id: int | None = None,
    branch: Callable[[int], str] | None = None,
) -> int:
    """Queue a run. `dispatch` is the dispatch minus `run_id`, which is the row's id.
    `triggered_by` is a GitHub login and `triggered_by_id` its numeric user id.
    `branch`, given the row's id, names the dispatch's `branch` (branch names
    include the run id)."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        run_id = conn.execute(
            """INSERT INTO runs (repo, task, ticket, workflow_id, attempt, retry_of,
                                 triggered_by, triggered_by_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
            (repo, task, ticket, workflow_id, attempt, retry_of, triggered_by, triggered_by_id,
             _now()),
        ).lastrowid
        conn.execute(
            "UPDATE runs SET dispatch = ? WHERE id = ?",
            (json.dumps({**dispatch, "run_id": run_id,
                         **({"branch": branch(run_id)} if branch else {})}), run_id),
        )
    return run_id


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def tick(
    conn: sqlite3.Connection,
    runner: Runner,
    *,
    limit: int,
    now: datetime | None = None,
    held: Collection[str] = (),
    paused: bool = False,
    check: Check | None = None,
    prepare: Prepare | None = None,
    gate: Callable[[datetime], int | None] | None = None,
) -> None:
    """Cancel what was asked, collect, then dispatch (unless `paused`). Queued
    rows of the repos in `held` stay queued. `gate`, asked after collecting,
    says how many rows may be dispatched (None: none; see `factory.brakes`).
    Then, if the runner has been unavailable for 30 minutes, that's a factory
    problem."""
    now = now or datetime.now(UTC)
    cancel_requested(conn, runner, now=now)
    collect(conn, runner, now=now, check=check)
    max_new = None if gate is None else gate(now)
    if not paused and (gate is None or max_new is not None):
        dispatch(conn, runner, limit=limit, held=held, now=now, prepare=prepare, max_new=max_new)
    _check_unavailable(conn, now)


def request_cancel(conn: sqlite3.Connection, run_id: int) -> bool:
    """Ask for an active row to be cancelled in the next tick. False if it's done."""
    with conn:
        changed = conn.execute(
            """UPDATE runs SET cancel_requested_at = coalesce(cancel_requested_at, ?)
               WHERE id = ? AND status IN ('queued', 'dispatched')""",
            (_now(), run_id),
        ).rowcount
    return changed == 1


def cancel_requested(
    conn: sqlite3.Connection, runner: Runner, *, now: datetime | None = None
) -> None:
    """Cancel each active row someone asked to cancel: `factory cancel`, or a
    human closing its issue or PR."""
    rows = conn.execute(
        """SELECT * FROM runs WHERE cancel_requested_at IS NOT NULL
           AND (status = 'queued' OR runner_run_id IS NOT NULL)
           AND status IN ('queued', 'dispatched') ORDER BY id"""
    ).fetchall()
    for row in rows:
        log.info("run %s: cancelling, as asked", row["id"], extra=_about(row))
        try:
            cancel(conn, runner, row)
        except RunnerUnavailable as e:
            # The request stays; the next tick tries again.
            _unavailable(conn, now)
            log.warning("runner unavailable while cancelling run %s: %s", row["id"], e,
                        extra=_about(row))


def collect(
    conn: sqlite3.Connection,
    runner: Runner,
    *,
    now: datetime | None = None,
    only: int | None = None,
    check: Check | None = None,
) -> None:
    """Collect finished runs, and cancel ones still running well past their timeout.

    `only` limits it to one run (`factory doctor` waiting on its smoke run).
    `check` judges a succeeded result further (see `Check`).
    """
    now = now or datetime.now(UTC)
    _release_stale_claims(conn, now)
    rows = conn.execute(
        """SELECT * FROM runs WHERE status = 'dispatched' AND runner_run_id IS NOT NULL
           AND (? IS NULL OR id = ?) ORDER BY id""",
        (only, only),
    ).fetchall()
    for row in rows:
        try:
            _collect_one(conn, runner, row, now, check)
        except TryLater as e:
            log.warning("run %s can't be judged yet, trying again next tick: %s", row["id"], e,
                        extra=_about(row))
        except RunRefused as e:
            # The run will never happen; not the ticket's fault, so not an attempt.
            # Dispatching backs off for a while, so it isn't refused every tick.
            log.warning("run %s goes back to the queue: %s", row["id"], e, extra=_about(row))
            _unavailable(conn, now)
            db_state.set_runner_refused_at(conn, _iso(now))
            with conn:
                conn.execute(
                    """UPDATE runs SET status = 'queued', runner_run_id = NULL, log_url = NULL,
                                      dispatched_at = NULL
                       WHERE id = ? AND status = 'dispatched'""",
                    (row["id"],),
                )
        except RunnerUnavailable as e:
            # The row stays dispatched until the runner answers.
            _unavailable(conn, now)
            log.warning("runner unavailable while collecting run %s: %s", row["id"], e,
                        extra=_about(row))


def _release_stale_claims(conn: sqlite3.Connection, now: datetime) -> None:
    """A row claimed for dispatching (`dispatch_run`) whose process died before
    the runner answered goes back to the queue."""
    rows = conn.execute(
        """SELECT * FROM runs WHERE status = 'dispatched' AND runner_run_id IS NULL
           AND dispatched_at < ?""",
        (_iso(now - CLAIM_STALE),),
    ).fetchall()
    for row in rows:
        log.warning("run %s was claimed for dispatching but never dispatched; queueing it again",
                    row["id"], extra=_about(row))
        _release(conn, row["id"])


def _collect_one(
    conn: sqlite3.Connection, runner: Runner, row: sqlite3.Row, now: datetime,
    check: Check | None = None,
) -> None:
    state = runner.state(row["repo"], row["runner_run_id"])
    if state is RunState.RUNNING:
        if _overdue(row, now):
            log.warning("run %s is still running past its timeout; cancelling it", row["id"],
                        extra=_about(row))
            cancel(conn, runner, row, outcome="timed_out")
        return
    if state is RunState.MISSING:
        log.warning("run %s is missing on the runner", row["id"], extra=_about(row))
        _finish(conn, row["id"], "failed", None)
    elif state is RunState.TIMED_OUT:
        log.warning("run %s hit its timeout on the runner", row["id"], extra=_about(row))
        _finish(conn, row["id"], "timed_out", None)
    else:
        files = runner.files(row["repo"], row["runner_run_id"])
        outcome, result, error = _judge(row, files, check)
        _finish(conn, row["id"], outcome, result, error)
    # A run ended on the runner, not refused: it runs runs again.
    db_state.set_runner_refused_at(conn, None)
    _available(conn)


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


def _judge(
    row: sqlite3.Row, files: RunFiles | None, check: Check | None = None
) -> tuple[str, str | None, str | None]:
    """The outcome of a finished run, the result.json to keep (as JSON text),
    and, for an invalid one, why."""
    if files is None or files.result_json is None:
        log.warning("run %s finished without a result.json", row["id"], extra=_about(row))
        return "failed", None, None
    try:
        data = json.loads(files.result_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning("run %s: result.json isn't JSON: %s", row["id"], e, extra=_about(row))
        return "invalid", None, f"result.json isn't JSON: {e}"
    kept = json.dumps(data)
    try:
        if not isinstance(data, dict):
            raise ContractError("result.json isn't an object")
        result = parse_result(data, task=row["task"])
        if result.run_id != row["id"]:
            raise ContractError(f"result.json is for run {result.run_id}")
        if (secret := find_secret(data.get("output"))) is not None:
            raise ContractError(f"output holds something that looks like a {secret}")
        _check_duplicate_of(row, data.get("output") or {})
        _check_blockers_first(row, data.get("output") or {})
        if check is not None and result.outcome == "succeeded":
            check(row, data, files)
    except ContractError as e:
        log.warning("run %s: invalid result.json: %s", row["id"], e, extra=_about(row))
        return "invalid", kept, str(e)
    return result.outcome, kept, None


def _check_duplicate_of(row: sqlite3.Row, output: dict[str, Any]) -> None:
    """Explore's `duplicate_of` must be one of the older issues it was sent."""
    duplicate_of = output.get("duplicate_of")
    if row["task"] != "explore" or duplicate_of is None:
        return
    sent = {i["ticket"] for i in json.loads(row["dispatch"]).get("older_issues", [])}
    if duplicate_of not in sent:
        raise ContractError(f"duplicate_of {duplicate_of} isn't one of the older issues sent")


def _check_blockers_first(row: sqlite3.Row, output: dict[str, Any]) -> None:
    """Auto-tickets lists blockers first: a ticket's `blocked_by` names only
    earlier tickets, so each index has an issue number by the time it's used."""
    if row["task"] != "auto-tickets":
        return
    for index, ticket in enumerate(output.get("tickets") or []):
        if any(blocker >= index for blocker in ticket.get("blocked_by", [])):
            raise ContractError(
                f"ticket {index}'s blocked_by {ticket['blocked_by']} names a ticket that isn't "
                "earlier in the list")


def _finish(
    conn: sqlite3.Connection, run_id: int, outcome: str, result: str | None,
    validation_error: str | None = None,
) -> None:
    """Mark a dispatched row done. `validation_error` says why an `invalid` one
    is (the retry's previous_attempt carries it)."""
    with conn:
        conn.execute(
            """UPDATE runs SET status = 'done', outcome = ?, result = ?, validation_error = ?,
                               finished_at = ?
               WHERE id = ? AND status = 'dispatched'""",
            (outcome, result, validation_error, _now(), run_id),
        )


def refused_until(conn: sqlite3.Connection, now: datetime) -> datetime | None:
    """When dispatching resumes after the runner refused a run, if it hasn't yet."""
    refused_at = db_state.runner_refused_at(conn)
    if refused_at is None:
        return None
    until = datetime.fromisoformat(refused_at) + REFUSED_BACKOFF
    return until if now < until else None


def dispatch(
    conn: sqlite3.Connection,
    runner: Runner,
    *,
    limit: int,
    held: Collection[str] = (),
    now: datetime | None = None,
    prepare: Prepare | None = None,
    max_new: int | None = None,
) -> None:
    """Dispatch queued rows, oldest first, while fewer than `limit` are in
    flight, skipping the repos in `held`, and at most `max_new` of them. Not
    while backing off after a refusal (`refused_until`)."""
    now = now or datetime.now(UTC)
    if refused_until(conn, now) is not None:
        return
    refused_at = db_state.runner_refused_at(conn)
    held = list(held)
    while max_new is None or max_new > 0:
        in_flight = conn.execute("SELECT count(*) FROM runs WHERE status = 'dispatched'").fetchone()[0]
        if in_flight >= limit:
            return
        row = conn.execute(
            f"""SELECT * FROM runs WHERE status = 'queued'
                AND repo NOT IN ({", ".join("?" * len(held))}) ORDER BY id LIMIT 1""",
            held,
        ).fetchone()
        if row is None:
            return
        try:
            dispatch_run(conn, runner, row, prepare)
            if max_new is not None:
                max_new -= 1
        except TryLater as e:
            log.warning("run %s can't be prepared, stays queued: %s", row["id"], e,
                        extra=_about(row))
            return
        except RunnerUnavailable as e:
            # The row stays queued; try again next tick.
            log.warning("runner unavailable, run %s stays queued: %s", row["id"], e,
                        extra=_about(row))
            _unavailable(conn, now)
            return
        if refused_at is None:
            # A dispatch accepted proves little while runs are being refused:
            # only a run that ends does then.
            _available(conn)


def dispatch_run(
    conn: sqlite3.Connection, runner: Runner, row: sqlite3.Row, prepare: Prepare | None = None
) -> bool:
    """Dispatch one queued row, whatever the limits. False if another process
    (`factory serve` and `factory doctor` share the database) claimed it first,
    so it isn't sent twice. Raises `RunnerUnavailable` or `TryLater` (the row
    stays queued then)."""
    sent = json.loads(row["dispatch"])
    if prepare is not None and (fresh := prepare(row)) is not None:
        sent = fresh
    # Claimed before the runner is asked: `dispatched` with no runner_run_id
    # yet. The row keeps what the run was actually sent.
    with conn:
        claimed = conn.execute(
            """UPDATE runs SET status = 'dispatched', dispatch = ?, dispatched_at = ?
               WHERE id = ? AND status = 'queued'""",
            (json.dumps(sent), _now(), row["id"]),
        ).rowcount
    if not claimed:
        log.info("run %s was dispatched by someone else", row["id"], extra=_about(row))
        return False
    try:
        runner_run_id = runner.dispatch(row["repo"], sent)
    except BaseException:
        _release(conn, row["id"])
        raise
    log_url = runner.log_link(row["repo"], runner_run_id)
    with conn:
        conn.execute(
            """UPDATE runs SET runner_run_id = ?, log_url = ?, dispatched_at = ?
               WHERE id = ?""",
            (runner_run_id, log_url, _now(), row["id"]),
        )
    return True


def _release(conn: sqlite3.Connection, run_id: int) -> None:
    """Put a claimed row that never reached the runner back in the queue."""
    with conn:
        conn.execute(
            """UPDATE runs SET status = 'queued', dispatched_at = NULL
               WHERE id = ? AND status = 'dispatched' AND runner_run_id IS NULL""",
            (run_id,),
        )


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _unavailable(conn: sqlite3.Connection, now: datetime | None) -> None:
    if db_state.runner_unavailable_since(conn) is None:
        db_state.set_runner_unavailable_since(conn, _iso(now or datetime.now(UTC)))


def _available(conn: sqlite3.Connection) -> None:
    if db_state.runner_unavailable_since(conn) is not None:
        log.info("the runner answers again")
        db_state.set_runner_unavailable_since(conn, None)
    if db_state.has_open_problem(conn, db_state.RUNNER_UNAVAILABLE):
        db_state.close_problem(conn, db_state.RUNNER_UNAVAILABLE)


def _check_unavailable(conn: sqlite3.Connection, now: datetime) -> None:
    """A runner unavailable for 30 minutes is a factory problem (#20 opens its issue)."""
    since = db_state.runner_unavailable_since(conn)
    if since is None or now - datetime.fromisoformat(since) < UNAVAILABLE_PROBLEM_AFTER:
        return
    if not db_state.has_open_problem(conn, db_state.RUNNER_UNAVAILABLE):
        log.error("the runner has been unavailable since %s", since)
        db_state.open_problem(conn, db_state.RUNNER_UNAVAILABLE,
                           f"The runner has been unavailable (or refusing runs) since {since}, "
                           "so nothing is dispatched or collected.")
