"""Attempts and retries (orchestrator.md#limits-and-brakes).

At most `MAX_ATTEMPTS` per ticket per task, counted from the last human
trigger (a trigger queues attempt 1). A run that ends `failed`, `invalid`,
`timed_out` or `superseded` is retried at once as a new row (`attempt` + 1,
`retry_of`), so it goes to the back of the queue. After the last attempt the
workflow gives up (`agent:blocked`).

Three failures aren't the ticket's fault, so they aren't attempts: the run is
queued again as a new row with the same `attempt` (#dispatching-and-collecting).
`contract_mismatch` also pauses dispatching everywhere, and `claude_auth`
holds dispatches for its repo; each opens a factory problem. Both last until
the orchestrator restarts (after a redeploy, or rotating the token): a run that
fails the same way again puts them back.
"""

import json
import logging
import sqlite3
from typing import Any

from factory import loop
from factory import state as db_state
from factory.logs import about_run

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
BAD = ("failed", "invalid", "timed_out", "superseded")
NOT_ATTEMPTS = ("contract_mismatch", "claude_auth", "usage_limit")
# Outcomes and failures with nothing useful to tell the next attempt.
_SILENT_OUTCOMES = ("timed_out", "superseded")
_SILENT_FAILURES = ("agent_timeout",)
# previous_attempt's check_output and validation_error are capped by the contract.
_MAX_TEXT = 20000


def failure(run: sqlite3.Row) -> str | None:
    """The run's `failure` reason, from its result.json, if it has one."""
    if run["outcome"] != "failed" or run["result"] is None:
        return None
    return json.loads(run["result"]).get("failure")


def is_bad(run: sqlite3.Row) -> bool:
    """The run ended badly and counts as an attempt."""
    return run["outcome"] in BAD and failure(run) not in NOT_ATTEMPTS


def not_an_attempt(conn: sqlite3.Connection, run: sqlite3.Row) -> bool:
    """If the run failed in a way that isn't an attempt, pause or hold
    dispatching as its failure asks and say True; the caller queues it again."""
    reason = failure(run)
    if reason not in NOT_ATTEMPTS:
        return False
    about = about_run(id=run["id"], repo=run["repo"], ticket=run["ticket"])
    if reason == "contract_mismatch":
        if not dispatch_paused(conn):
            log.error("run %s failed with contract_mismatch: the runner doesn't accept this "
                      "orchestrator's contract_version. Dispatching is paused until the "
                      "orchestrator is redeployed and restarted.", run["id"], extra=about)
        db_state.open_problem(
            conn, "contract_mismatch",
            "The runner rejected the dispatch's contract_version, so dispatching is paused. "
            "Merge the schema change, then redeploy and restart the orchestrator.")
    elif reason == "claude_auth":
        if run["repo"] not in held_repos(conn):
            log.error("run %s failed with claude_auth: dispatches for %s are held until the "
                      "orchestrator restarts", run["id"], run["repo"], extra=about)
        db_state.open_problem(
            conn, "claude_auth",
            f"Claude rejected the token on {run['repo']} (expired, revoked or missing), so its "
            "runs are held. Set the CLAUDE_CODE_OAUTH_TOKEN secret, then restart the orchestrator.",
            repo=run["repo"])
    else:
        log.info("run %s hit the Claude usage limit; queueing it again", run["id"], extra=about)
    return True


def dispatch_paused(conn: sqlite3.Connection) -> bool:
    """A `contract_mismatch` has paused dispatching."""
    return any(p["kind"] == "contract_mismatch" for p in db_state.open_problems(conn))


def held_repos(conn: sqlite3.Connection) -> set[str]:
    """Repos whose dispatches `claude_auth` holds."""
    return {p["repo"] for p in db_state.open_problems(conn) if p["kind"] == "claude_auth"}


def lift_holds(conn: sqlite3.Connection) -> None:
    """At startup: a restart is how a human says the cause is fixed."""
    for problem in db_state.open_problems(conn):
        if problem["kind"] in ("contract_mismatch", "claude_auth"):
            log.info("lifting the %s hold%s at startup", problem["kind"],
                     f" on {problem['repo']}" if problem["repo"] else "")
            db_state.close_problem(conn, problem["kind"], repo=problem["repo"])


def retry(conn: sqlite3.Connection, run: sqlite3.Row) -> int | None:
    """Queue the next attempt of a bad run (its row id), or None if it was the last one."""
    if run["attempt"] >= MAX_ATTEMPTS:
        return None
    return requeue(conn, run, attempt=run["attempt"] + 1, previous=_previous_attempt(run))


def requeue(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    *,
    attempt: int,
    previous: dict[str, Any] | None | bool = False,
) -> int:
    """Queue `run` again as a new row with `attempt`. `previous` replaces the
    dispatch's `previous_attempt` (None drops it); False keeps it as it was.
    If a row retrying `run` already exists (a crash before `run` was marked
    handled), that row's id: it's never queued twice."""
    existing = conn.execute("SELECT id FROM runs WHERE retry_of = ?", (run["id"],)).fetchone()
    if existing is not None:
        return existing["id"]
    dispatch = json.loads(run["dispatch"])
    dispatch.pop("run_id", None)
    if previous is not False:
        dispatch.pop("previous_attempt", None)
        if previous is not None:
            dispatch["previous_attempt"] = previous
    branch = dispatch.pop("branch", None)
    suffix = f"-{run['id']}"
    if branch is not None and not (run["task"] == "implement" and branch.endswith(suffix)):
        # Not named after the run (e.g. review's PR branch): keep it.
        dispatch["branch"] = branch
        branch = None

    def rename(run_id: int) -> str:
        assert branch is not None
        return f"{branch.removesuffix(suffix)}-{run_id}"

    run_id = loop.enqueue(
        conn, repo=run["repo"], task=run["task"], dispatch=dispatch, ticket=run["ticket"],
        workflow_id=run["workflow_id"], attempt=attempt, retry_of=run["id"],
        triggered_by=run["triggered_by"], triggered_by_id=run["triggered_by_id"],
        branch=rename if branch is not None else None,
    )
    log.info("queued run %s (attempt %s) after run %s ended %s", run_id, attempt, run["id"],
             failure(run) or run["outcome"],
             extra=about_run(id=run_id, repo=run["repo"], ticket=run["ticket"]))
    return run_id


def _previous_attempt(run: sqlite3.Row) -> dict[str, Any] | None:
    """What attempt 2's dispatch says about `run`, or None when it has nothing
    useful to say."""
    reason = failure(run)
    if run["outcome"] in _SILENT_OUTCOMES or reason in _SILENT_FAILURES:
        return None
    if run["outcome"] == "invalid":
        return {"outcome": "invalid",
                "validation_error": (run["validation_error"] or "unknown")[-_MAX_TEXT:]}
    if reason is None:
        return None  # missing, or no result.json: nothing to say
    previous: dict[str, Any] = {"outcome": "failed", "failure": reason}
    check = _check_output(run)
    if check:
        previous["check_output"] = check[-_MAX_TEXT:]
    return previous


def _check_output(run: sqlite3.Row) -> str | None:
    if run["result"] is None:
        return None
    data = json.loads(run["result"])
    check = data.get("check_after") or data.get("check")
    return check.get("output_tail") if isinstance(check, dict) else None
