"""Factory problem issues (orchestrator.md#observability).

A problem that belongs to no ticket is a `problems` row (`factory.state`),
opened and closed by whoever spots it: `factory.attempts` (`contract_mismatch`,
`claude_auth`), `factory.loop` (the runner unavailable), the poller (polls
failing), `factory.enrolment` (an invalid enrolment) and `check_usage` here.
`sync` mirrors those rows onto GitHub, one issue per open row, assigned to the
factory's `owner`:

- a problem about the whole factory (`repo` None) goes on the factory repo and
  names no enrolled repo; one about a repo goes on that repo;
- a changed summary is posted as a comment;
- a cleared problem's issue gets a comment and is closed. One that cleared
  before its issue could be opened (a repo whose API was down) is opened and
  closed at once, so it's still on record.

Closing the issue by hand changes nothing: a `claude_auth` hold and a
`contract_mismatch` pause last until the orchestrator restarts (#16).

The issue never holds a raw error body: summaries are the factory's own words,
and every body is passed through `tokenscan.redact` too. A write that fails is
logged and tried again on the next pass.
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from factory import state as db_state
from factory import tokenscan
from factory.github import GitHub, GitHubError

log = logging.getLogger(__name__)

TITLES = {
    "usage_unreadable": "Factory problem: Claude usage can't be read",
    "contract_mismatch": "Factory problem: contract_mismatch, dispatching is paused",
    "runner_unavailable": "Factory problem: the runner is unavailable",
    "poll_failing": "Factory problem: polling this repo is failing",
    "claude_auth": "Factory problem: Claude auth is failing on this repo",
    "invalid_enrolment": "Factory problem: .factory/config.yml fails its schema",
}


# The factory has failed closed this long before usage that can't be read is a problem.
USAGE_UNREADABLE_AFTER = timedelta(minutes=15)


def check_usage(conn: sqlite3.Connection, now: datetime) -> None:
    """Open the `usage_unreadable` problem once usage has been unreadable for
    15 minutes, and close it when a reading succeeds."""
    since = db_state.usage_unreadable_since(conn)
    is_open = any(p["kind"] == "usage_unreadable" for p in db_state.open_problems(conn))
    if since is None:
        if is_open:
            db_state.close_problem(conn, "usage_unreadable")
        return
    if now - datetime.fromisoformat(since) < USAGE_UNREADABLE_AFTER:
        return
    reading = db_state.latest_usage(conn)
    error = reading.error if reading is not None else None
    if not is_open:
        log.error("Claude usage has been unreadable since %s: %s", since, error)
    db_state.open_problem(
        conn, "usage_unreadable",
        f"Claude usage hasn't been readable since {since} (latest: {error}), so nothing is "
        "dispatched: the factory fails closed. Check `claude -p /usage` as the factory user.")


def sync(conn: sqlite3.Connection, github: GitHub, *, factory_repo: str) -> None:
    """Bring every problem's GitHub issue up to date with its row."""
    owner = db_state.owner(conn)
    for problem in db_state.unsynced_problems(conn):
        where = problem["repo"] or factory_repo
        try:
            _sync(conn, github, problem, where, owner)
        except (GitHubError, OSError) as e:
            if problem["closed_at"] is not None and getattr(e, "status", None) in (403, 404):
                # The App lost access to the repo: there's nothing left to close.
                log.warning("can't close the %s problem issue on %s (%s); dropping it",
                            problem["kind"], where, e, extra={"repo": problem["repo"]})
                db_state.problem_issue_closed(conn, problem["id"])
                continue
            log.warning("writing the %s problem issue on %s failed, trying again next pass: %s",
                        problem["kind"], where, e, extra={"repo": problem["repo"]})


def _sync(
    conn: sqlite3.Connection, github: GitHub, problem: sqlite3.Row, where: str, owner: str | None
) -> None:
    summary = problem["summary"]
    if problem["issue"] is None:
        number = github.create_issue(
            where, TITLES.get(problem["kind"], f"Factory problem: {problem['kind']}"),
            _body(problem["kind"], summary), [], assignees=(owner,) if owner else (),
        )
        db_state.problem_posted(conn, problem["id"], f"{where}#{number}", summary)
        log.info("opened %s#%s for the %s problem", where, number, problem["kind"],
                 extra={"repo": problem["repo"]})
    else:
        number = int(problem["issue"].rsplit("#", 1)[1])
        if problem["posted"] != summary:
            github.post_comment(where, number, _redact(summary))
            db_state.problem_posted(conn, problem["id"], problem["issue"], summary)
    if problem["closed_at"] is not None:
        github.post_comment(where, number, f"Cleared at {problem['closed_at']}.")
        github.close_issue(where, number)
        db_state.problem_issue_closed(conn, problem["id"])
        log.info("closed %s#%s: the %s problem cleared", where, number, problem["kind"],
                 extra={"repo": problem["repo"]})


def _body(kind: str, summary: str) -> str:
    return (f"<!-- factory:problem kind={kind} -->\n{_redact(summary)}\n\n"
            "The factory closes this issue itself when the problem clears.")


def _redact(text: str) -> str:
    return tokenscan.redact(text)
