"""The orchestrator's work on GitHub: finding triggers and posting results.

`run_once` is one pass of the main loop: poll each repo for trigger labels,
`loop.tick` the queue (collect, then dispatch), then act on finished runs.
`serve` runs it about every 60 seconds, and re-reads the enrolments (which
repos are polled, see `factory.enrolment`) every 10 minutes.

Every trigger label is checked (`factory.triggers`); only explore is queued so far.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from factory import enrolment as enrolments
from factory import loop, tickets, triggers
from factory import state as db_state
from factory.contract import CONTRACT_VERSION
from factory.enrolment import Enrolment
from factory.github import GitHub, GitHubError
from factory.logs import about_run
from factory.runner import Runner

log = logging.getLogger(__name__)

# Per-task timeouts in minutes, for the whole job. They live here only and travel
# in each dispatch; a run still running 15 minutes past its timeout is cancelled.
DEFAULT_TIMEOUTS = {"explore": 20, "auto-tickets": 20, "implement": 45, "review": 45}


@dataclass(frozen=True)
class Settings:
    """The global config. An enrolment's `timeouts` and `models` override the
    per-task defaults for its repo; the rest stays global."""

    # The factory's own repo, whose main holds factory-config.yml (`owner`).
    factory_repo: str = "ScottMRedmond/swfactory"
    poll_seconds: int = 60
    refresh_seconds: int = 600  # how often installations and enrolments are re-read
    max_in_flight: int = 2
    per_issue_concurrency: int = 1  # active runs per issue or PR, whatever the task
    timeouts: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    models: dict[str, str] = field(default_factory=lambda: {"explore": "claude-sonnet-4-6"})

    def timeout(self, task: str, enrolment: Enrolment) -> int:
        return enrolment.config.timeouts.get(task, self.timeouts[task])

    def model(self, task: str, enrolment: Enrolment) -> str:
        return enrolment.config.models.get(task, self.models[task])


def poll(conn: sqlite3.Connection, github: GitHub, repo: str, settings: Settings) -> None:
    """A full scan: look at every open issue and PR in the repo for triggers."""
    handle_triggers(conn, github, repo, github.open_issues(repo), settings)


def handle_triggers(
    conn: sqlite3.Connection,
    github: GitHub,
    repo: str,
    issues: list[dict[str, Any]],
    settings: Settings,
) -> None:
    """Check the trigger labels on each issue and PR a poll found, and queue
    what they ask for (see `factory.triggers` for what's rejected and why).

    `issues` is what a poll found: a full scan's open issues and PRs, or the
    issues and PRs (open or closed) updated since the cursor.

    Only explore is queued so far; an accepted `agent:implement`, `agent:review`
    or `agent:implement-spec` is left on its issue for the tickets that build
    those tasks. The row is inserted first, then the label removed, so a crash
    between the two leaves a duplicate trigger rather than a lost one, which
    the next poll removes quietly. A ticket too big to dispatch gets a notice
    instead, and its label is removed too.

    Only an enrolled repo is polled. The first poll of a repo creates the
    factory labels it lacks.
    """
    enrolment = enrolments.get(conn, repo)
    if enrolment is None:
        raise LookupError(f"{repo} isn't enrolled")
    enrolments.ensure_labels(conn, github, repo)
    for issue in issues:
        decision = triggers.check(
            github, repo, issue, enrolment,
            active=_active_on(conn, repo, issue["number"]),
            per_issue_concurrency=settings.per_issue_concurrency,
        )
        if decision is None:
            continue
        if isinstance(decision, triggers.Rejection):
            _reject(github, repo, issue, decision)
        elif decision.task == "explore":
            _queue_explore(conn, github, repo, issue, enrolment, settings, decision.triggered_by)
            github.remove_label(repo, issue["number"], decision.label)


def _queue_explore(
    conn: sqlite3.Connection,
    github: GitHub,
    repo: str,
    issue: dict[str, Any],
    enrolment: Enrolment,
    settings: Settings,
    triggered_by: str | None,
) -> None:
    ticket = str(issue["number"])
    head = github.default_branch_head(repo)
    try:
        dispatch = tickets.fit(
            {
                "contract_version": CONTRACT_VERSION,
                "repo": repo,
                "task": "explore",
                "ticket": ticket,
                "older_issues": tickets.older_issues(github, repo, issue["number"]),
                "base_sha": head,
                "enrolment_sha": enrolment.sha,
                "model": settings.model("explore", enrolment),
                "timeout_minutes": settings.timeout("explore", enrolment),
            },
            tickets.fetch(github, repo, issue),
        )
    except tickets.TooBig as e:
        github.post_comment(repo, issue["number"], render_too_big_notice(str(e), triggered_by))
        log.info("%s#%s is too big to dispatch: %s", repo, ticket, e)
        return
    run_id = loop.enqueue(conn, repo=repo, task="explore", ticket=ticket, dispatch=dispatch,
                          triggered_by=triggered_by)
    log.info("queued explore run %s for %s#%s", run_id, repo, ticket,
             extra=about_run(id=run_id, repo=repo, ticket=ticket))


def _active_on(conn: sqlite3.Connection, repo: str, number: int) -> list[sqlite3.Row]:
    """The queued and dispatched runs on an issue or PR: its ticket's, and those
    whose dispatch names it as their PR."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE repo = ? AND status IN ('queued', 'dispatched') ORDER BY id",
        (repo,),
    ).fetchall()
    return [r for r in rows
            if r["ticket"] == str(number) or json.loads(r["dispatch"]).get("pr_number") == number]


def _reject(
    github: GitHub, repo: str, issue: dict[str, Any], rejection: triggers.Rejection
) -> None:
    """Post the rejection's notice, if it has one, then remove its labels."""
    number = issue["number"]
    if rejection.message is not None:
        github.post_comment(repo, number, render_notice(rejection.message, rejection.mention))
        log.info("rejected %s on %s#%s: %s", ", ".join(rejection.labels), repo, number,
                 rejection.message, extra={"repo": repo, "ticket": str(number)})
    else:
        log.info("removed %s from %s#%s", ", ".join(rejection.labels), repo, number,
                 extra={"repo": repo, "ticket": str(number)})
    for label in rejection.labels:
        github.remove_label(repo, number, label)


def render_notice(message: str, mention: list[str] | None = None) -> str:
    """A notice: a new comment, marked, @mentioning who has to act."""
    who = "".join(f"@{login} " for login in mention or [])
    return f"{tickets.NOTICE_MARK}\n{who}{message}\n"


def cancel_closed(
    conn: sqlite3.Connection, repo: str, issues: list[dict[str, Any]], *, full_scan: bool
) -> None:
    """Ask for the active rows of every issue or PR a human has closed to be cancelled.

    `issues` is what the poll found. In a full scan (open issues and PRs only),
    an issue or PR with active rows that isn't in the list has been closed. In an
    incremental poll, a closed one shows up with `state: closed`. A row belongs
    to its ticket and, for runs on a PR, to the PR in its dispatch.

    The cancel itself happens in `loop.tick`, before collecting, so the runner
    cancels dispatched runs and whatever they leave behind is discarded.
    """
    if full_scan:
        open_numbers = {str(i["number"]) for i in issues}

        def closed(number: str) -> bool:
            return number not in open_numbers
    else:
        closed_numbers = {str(i["number"]) for i in issues if i.get("state") == "closed"}

        def closed(number: str) -> bool:
            return number in closed_numbers

    rows = conn.execute(
        """SELECT * FROM runs WHERE repo = ? AND status IN ('queued', 'dispatched')
           AND cancel_requested_at IS NULL ORDER BY id""",
        (repo,),
    ).fetchall()
    for row in rows:
        subjects = [row["ticket"], json.loads(row["dispatch"]).get("pr_number")]
        if any(s is not None and closed(str(s)) for s in subjects):
            log.info("run %s: %s#%s was closed; cancelling", row["id"], repo, row["ticket"],
                     extra=_about(row))
            loop.request_cancel(conn, row["id"])


def render_too_big_notice(reason: str, mention: str | None = None) -> str:
    """The notice for a ticket whose title and body alone don't fit in a dispatch.

    `mention` is the login that triggered the run.
    """
    return render_notice(
        f"This ticket's title and body are too long to dispatch ({reason}). "
        "Please shorten it, for example by moving logs or long examples into a linked "
        "file or gist, then add the label again.",
        [mention] if mention else [],
    )


def _about(row: sqlite3.Row) -> dict[str, Any]:
    return about_run(id=row["id"], repo=row["repo"], ticket=row["ticket"])


# --- polling -------------------------------------------------------------------

# The `since` query reaches this far back before the cursor: the issue list can
# lag a label change by some seconds.
OVERLAP_SECONDS = 2 * 60
# A gap longer than this between polls means the orchestrator was down or the
# machine asleep: scan everything again and fetch fresh tokens.
WAKE_GAP_SECONDS = 5 * 60
MAX_BACKOFF_SECONDS = 15 * 60
# After failing this long, a repo's polling problem is logged as an error.
FAILING_ALERT_SECONDS = 30 * 60


@dataclass
class RepoPoll:
    """One repo's polling state. It lives in memory only."""

    cursor: float | None = None  # start of the last poll that got an answer
    etag: str | None = None
    wait: float = 0  # seconds between polls; 0 until the first poll
    next_at: float = 0
    failing_since: float | None = None
    alerted: bool = False


class Poller:
    """Decides, per repo, whether this pass polls, and how.

    - The first poll of a repo, and any poll after a gap of more than 5
      minutes since the previous pass, is a full scan of every open issue and
      PR, with fresh App tokens. Otherwise it's one conditional
      `issues?state=all&since=<cursor - 2 min>&sort=updated` call.
    - A 5xx or network error doubles the repo's wait, up to 15 minutes; one
      success drops it back to `poll_seconds`. A 403/429 rate limit waits as
      long as `Retry-After` or `x-ratelimit-reset` says.
    - After 30 minutes of failures, it logs an error (once per failing spell).
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._repos: dict[str, RepoPoll] = {}
        self._last_pass: float | None = None

    def poll_all(self, conn: sqlite3.Connection, github: GitHub, settings: Settings) -> None:
        now = self._clock()
        if self._last_pass is None:
            github.forget_tokens()
        elif now - self._last_pass > WAKE_GAP_SECONDS:
            log.info("%.0f s since the last poll: scanning every repo again", now - self._last_pass)
            github.forget_tokens()
            for state in self._repos.values():
                state.cursor = state.etag = None
        self._last_pass = now
        for repo in [e.repo for e in enrolments.polled(conn)]:
            state = self._repos.setdefault(repo, RepoPoll())
            if now < state.next_at:
                continue
            self._poll_repo(conn, github, repo, state, settings, now)

    def _poll_repo(
        self,
        conn: sqlite3.Connection,
        github: GitHub,
        repo: str,
        state: RepoPoll,
        settings: Settings,
        now: float,
    ) -> None:
        try:
            if state.cursor is None:
                issues = github.open_issues(repo)
                etag = state.etag
            else:
                since = datetime.fromtimestamp(state.cursor - OVERLAP_SECONDS, UTC)
                issues, etag = github.issues_since(
                    repo, since.strftime("%Y-%m-%dT%H:%M:%SZ"), state.etag
                )
            if issues is not None:
                cancel_closed(conn, repo, issues, full_scan=state.cursor is None)
                handle_triggers(conn, github, repo, issues, settings)
                state.cursor, state.etag = now, etag
        except GitHubError as e:
            self._failed(repo, state, settings, now, e, _rate_limit_until(e, now))
            return
        except OSError as e:
            self._failed(repo, state, settings, now, e, None)
            return
        state.wait = settings.poll_seconds
        state.next_at = now + state.wait
        state.failing_since, state.alerted = None, False
        db_state.record_poll(conn, repo)

    def _failed(
        self,
        repo: str,
        state: RepoPoll,
        settings: Settings,
        now: float,
        error: Exception,
        until: float | None,
    ) -> None:
        if until is not None:
            state.next_at = until
            log.warning("polling %s was rate limited until %s: %s", repo,
                        datetime.fromtimestamp(until, UTC).isoformat(timespec="seconds"), error,
                        extra={"repo": repo})
        else:
            state.wait = min(max(state.wait, settings.poll_seconds) * 2, MAX_BACKOFF_SECONDS)
            state.next_at = now + state.wait
            log.warning("polling %s failed, next try in %.0f s: %s", repo, state.wait, error,
                        extra={"repo": repo})
        if state.failing_since is None:
            state.failing_since = now
        if not state.alerted and now - state.failing_since >= FAILING_ALERT_SECONDS:
            state.alerted = True
            log.error("polling %s has failed for %.0f minutes: %s",
                      repo, (now - state.failing_since) / 60, error, extra={"repo": repo})


def _rate_limit_until(error: GitHubError, now: float) -> float | None:
    """When a 403/429 rate limit lifts, or None if `error` isn't a rate limit."""
    if error.status not in (403, 429):
        return None
    retry_after = error.headers.get("retry-after")
    if retry_after is not None:
        try:
            return now + float(retry_after)
        except ValueError:
            pass
    reset = error.headers.get("x-ratelimit-reset")
    if reset is not None and (error.headers.get("x-ratelimit-remaining") == "0" or error.status == 429):
        try:
            return max(float(reset), now)
        except ValueError:
            pass
    return None


def run_once(
    conn: sqlite3.Connection,
    github: GitHub,
    runner: Runner,
    settings: Settings,
    *,
    poller: Poller | None = None,
) -> None:
    """One pass: poll every enrolled repo that's due, tick the queue, act on
    finished runs.

    `poller` holds the polling state between passes; without one, every repo
    gets a full scan. A repo whose calls fail backs off; the others carry on.
    Queued runs of a repo with `enabled: false` (or no longer enrolled) wait.
    """
    (poller or Poller()).poll_all(conn, github, settings)
    loop.tick(conn, runner, limit=settings.max_in_flight, held=enrolments.held(conn))
    handle_finished(conn, github, runner)


def handle_finished(conn: sqlite3.Connection, github: GitHub, runner: Runner) -> None:
    """Write each done run's result to GitHub, once.

    A row is marked handled only after its writes succeed, so a failed post
    is tried again on the next pass.
    """
    rows = conn.execute(
        "SELECT * FROM runs WHERE status = 'done' AND handled_at IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        try:
            if row["task"] == "explore" and row["outcome"] == "succeeded":
                result = json.loads(row["result"])
                link = runner.log_link(row["repo"], row["runner_run_id"])
                body = render_explore_comment(row["id"], result["output"], link)
                github.post_comment(row["repo"], int(row["ticket"]), body)
                log.info("posted explore findings of run %s on %s#%s",
                         row["id"], row["repo"], row["ticket"], extra=_about(row))
        except (GitHubError, OSError) as e:
            log.error("run %s: writing the result to GitHub failed: %s", row["id"], e,
                      extra=_about(row))
            continue
        with conn:
            conn.execute(
                "UPDATE runs SET handled_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(timespec="seconds"), row["id"]),
            )


def render_explore_comment(run_id: int, output: dict[str, Any], log_link: str) -> str:
    """Explore's findings under the factory's own headings, the same every time."""

    def section(heading: str, text: str) -> str:
        return f"### {heading}\n\n{text.strip()}\n"

    def listed(items: list[str], numbered: bool) -> str:
        if not items:
            return "None."
        return "\n".join(f"{i}. {x}" if numbered else f"- {x}" for i, x in enumerate(items, 1))

    parts = [
        f"<!-- factory:explore run={run_id} -->\n## Explore findings\n",
        f"**Verdict:** `{output['verdict']}`\n",
        section("Summary", output["summary"]),
        section("Findings", output["findings"]),
        section("Plan", listed(output["plan"], numbered=True)),
        section("Files", listed([f"`{f}`" for f in output["files"]], numbered=False)),
    ]
    if output.get("questions"):
        parts.append(section("Questions", listed(output["questions"], numbered=True)))
    if output.get("duplicate_of"):
        parts.append(section("Duplicate of", f"#{output['duplicate_of']}"))
    parts.append(f"---\nFactory run {run_id} · [runner log]({log_link})\n")
    return "\n".join(parts)


def serve(
    conn: sqlite3.Connection,
    github: GitHub,
    runner: Runner,
    settings: Settings,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> None:
    """Run `run_once` about every `poll_seconds`, until interrupted.

    Enrolments (and the factory's `owner`) are re-read at the start and then
    every `refresh_seconds`. A failed refresh is logged, the last known
    enrolments are used, and it's tried again on the next pass.
    """
    log.info("serving, polling every %ss", settings.poll_seconds)
    refreshed_at: float | None = None
    poller = Poller(clock)
    while True:
        if refreshed_at is None or clock() - refreshed_at >= settings.refresh_seconds:
            try:
                owner = enrolments.refresh(conn, github, factory_repo=settings.factory_repo)
                refreshed_at = clock()
                log.info("enrolments refreshed: polling %s; owner %s",
                         ", ".join(e.repo for e in enrolments.polled(conn)) or "no repos", owner)
            except Exception:
                log.exception("refreshing the enrolments failed; trying again next pass")
        try:
            run_once(conn, github, runner, settings, poller=poller)
        except Exception:
            log.exception("the loop failed; trying again next poll")
        sleep(settings.poll_seconds)
