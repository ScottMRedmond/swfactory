"""The orchestrator's work on GitHub: finding triggers and posting results.

`run_once` is one pass of the main loop: poll each repo for trigger labels,
`loop.tick` the queue (collect, then dispatch), then act on finished runs.
`serve` runs it about every 60 seconds, and re-reads the enrolments (which
repos are polled, see `factory.enrolment`) every 10 minutes.

Every trigger label is checked (`factory.triggers`) and an accepted one
queued. What a finished run leads to is its workflow's business (`factory.workflows`).
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory import (attempts, brakes, bundles, loop, problems, review, status_comment, triggers,
                     usage, workflows)
from factory import enrolment as enrolments
from factory import state as db_state
from factory.enrolment import Enrolment
from factory.github import GitHub, GitHubError
from factory.logs import about_row
from factory.runner import Runner
from factory.tickets import render_notice
from factory.workflows import implement_spec, implement_ticket

log = logging.getLogger(__name__)

# Per-task timeouts in minutes, for the whole job. They live here only and travel
# in each dispatch; a run still running 15 minutes past its timeout is cancelled.
DEFAULT_TIMEOUTS = {"explore": 20, "auto-tickets": 20, "implement": 45, "review": 45}
# Explore and auto-tickets mostly read and judge, so they get a cheaper model;
# implement and review the strongest (orchestrator.md#limits-and-brakes).
DEFAULT_MODELS = {"explore": "claude-sonnet-4-6", "auto-tickets": "claude-sonnet-4-6",
                  "implement": "claude-opus-4-6", "review": "claude-opus-4-6"}


@dataclass(frozen=True)
class Settings:
    """The global config (orchestrator.md#limits-and-brakes). An enrolment's
    `timeouts` and `models` override the per-task defaults for its repo; the
    rest, `limits` included, stays global."""

    # The factory's own repo, whose main holds factory-config.yml (`owner`).
    factory_repo: str = "ScottMRedmond/swfactory"
    poll_seconds: int = 60
    refresh_seconds: int = 600  # how often installations and enrolments are re-read
    # Concurrency, the daily cap and the usage thresholds: global, never per repo.
    limits: brakes.Limits = field(default_factory=brakes.Limits)
    per_issue_concurrency: int = 1  # active runs per issue or PR, whatever the task
    # Tickets of one implement-spec workflow with implement queued or running.
    per_spec_concurrency: int = 1
    timeouts: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    # Where bundles are checked and pushed from: a bare repo per target repo.
    git_dir: Path = field(default_factory=lambda: Path.home() / ".factory" / "git")

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

    Explore, implement, review and implement-spec are queued (implement not
    while the ticket has an open factory PR; a review of a PR the factory
    didn't open is findings-only, and a fork PR gets only a notice;
    implement-spec not while the spec has open tickets). The row is inserted
    first, then the label removed, so a crash between the two leaves a
    duplicate trigger rather than a lost one, which the next poll removes
    quietly. A ticket too big to dispatch gets a notice
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
            implement_ticket.start_explore(conn, github, settings, issue, enrolment,
                                           decision.triggered_by)
            github.remove_label(repo, issue["number"], decision.label)
        elif decision.task == "implement" and implement_spec.is_spec_ticket(issue):
            # Built on its spec's integration branch, not as a ticket of its own.
            implement_spec.start_ticket(conn, github, settings, issue, enrolment,
                                        decision.triggered_by)
            github.remove_label(repo, issue["number"], decision.label)
        elif decision.task == "implement":
            implement_ticket.start_implement(conn, github, settings, issue, enrolment,
                                             decision.triggered_by)
            github.remove_label(repo, issue["number"], decision.label)
        elif decision.task == "review":
            _start_review(conn, github, settings, issue, enrolment, decision)
            github.remove_label(repo, issue["number"], decision.label)
        elif decision.task == "implement-spec":
            implement_spec.start(conn, github, settings, issue, enrolment, decision.triggered_by)
            github.remove_label(repo, issue["number"], decision.label)


FORK_NOTICE = ("This pull request comes from a fork, and the factory reviews only pull requests "
               "from branches of this repo: a review runs the PR's own code (its tests and setup "
               "scripts) next to the factory's Claude token. So `agent:review` was removed and "
               "nothing was started.")


def _start_review(
    conn: sqlite3.Connection, github: GitHub, settings: Settings, issue: dict[str, Any],
    enrolment: Enrolment, decision: triggers.Trigger,
) -> None:
    """`agent:review` on a PR: a factory PR (a ticket's or a spec's) is reviewed
    again in its workflow, any other gets a findings-only review, and a fork PR
    nothing but a notice."""
    repo, number = enrolment.repo, issue["number"]
    if review.is_fork(github.pull(repo, number), repo):
        who = decision.triggered_by
        github.post_comment(repo, number,
                            render_notice(FORK_NOTICE, [who.login] if who else None))
        log.info("%s#%s is a fork PR: not reviewed", repo, number, extra={"repo": repo})
        return
    workflows.start_review(conn, github, settings, issue, enrolment, decision.triggered_by)


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
                     extra=about_row(row))
            loop.request_cancel(conn, row["id"])



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
    - After 30 minutes of failures, it logs an error (once per failing spell)
      and opens the repo's `poll_failing` factory problem, closed by the next
      successful poll. Its issue is opened once the repo's API answers.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._repos: dict[str, RepoPoll] = {}
        self._last_pass: float | None = None

    def poll_all(self, conn: sqlite3.Connection, github: GitHub, settings: Settings) -> bool:
        """Poll every repo that's due. True on startup or waking, when the
        caller should take a fresh usage reading too."""
        now = self._clock()
        woke = self._last_pass is None or now - self._last_pass > WAKE_GAP_SECONDS
        if self._last_pass is None:
            github.forget_tokens()
        elif woke:
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
        return woke

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
                workflows.cancel_closed_prs(conn, github, settings, repo, issues,
                                            full_scan=state.cursor is None)
                handle_triggers(conn, github, repo, issues, settings)
                state.cursor, state.etag = now, etag
        except GitHubError as e:
            self._failed(conn, repo, state, settings, now, e, _rate_limit_until(e, now))
            return
        except OSError as e:
            self._failed(conn, repo, state, settings, now, e, None)
            return
        state.wait = settings.poll_seconds
        state.next_at = now + state.wait
        state.failing_since, state.alerted = None, False
        db_state.record_poll(conn, repo)
        if db_state.has_open_problem(conn, db_state.POLL_FAILING, repo=repo):
            db_state.close_problem(conn, db_state.POLL_FAILING, repo=repo)

    def _failed(
        self,
        conn: sqlite3.Connection,
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
            since = datetime.fromtimestamp(state.failing_since, UTC).isoformat(timespec="seconds")
            # The problem issue says what kind of error it was, never GitHub's message.
            what = f"HTTP {error.status}" if isinstance(error, GitHubError) else type(error).__name__
            db_state.open_problem(
                conn, db_state.POLL_FAILING,
                f"Polling this repo has failed since {since} ({what}), so new trigger labels "
                "aren't seen. The factory keeps retrying and closes this when a poll succeeds.",
                repo=repo)


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
    Queued runs of a repo with `enabled: false` (or no longer enrolled), or
    held for `claude_auth`, wait, and so does the whole queue while a brake
    is on (`factory.brakes`: manual pause, `contract_mismatch`, a runner
    refusal, the daily cap, usage): polling, cancelling, collecting and posting
    carry on. With a poller, a startup or wake-up pass also takes a fresh
    usage reading.
    """
    woke = (poller or Poller()).poll_all(conn, github, settings)
    held = enrolments.held(conn)
    ctx = workflows.Context(conn, github, settings, runner)
    if woke and poller is not None:
        usage.refresh(conn, settings.limits, datetime.now(UTC), force=True)
    workflows.advance(ctx)
    loop.tick(conn, runner, limit=settings.limits.max_in_flight, held=held,
              check=_bundle_check(conn, github, settings),
              prepare=_try_later(lambda row: workflows.prepare(ctx, row)),
              gate=lambda now: _gate(conn, settings.limits, held, now))
    handle_finished(conn, github, runner, settings)
    status_comment.sync(conn, github, held=held, limits=settings.limits)
    problems.check_usage(conn, datetime.now(UTC))
    problems.sync(conn, github, factory_repo=settings.factory_repo)


def _gate(
    conn: sqlite3.Connection, limits: brakes.Limits, held: dict[str, str], now: datetime
) -> int | None:
    """How many rows the brakes let this pass dispatch (the daily cap's
    remainder), or None for none (see `factory.brakes`).

    The usage reading is taken here, just before dispatching, and only when
    something could be dispatched: no other brake is on, a slot is free and a
    row of a repo that isn't held is queued. A reading is reused for 5 minutes.
    """
    if brakes.global_hold(conn, limits, now, usage=False) is not None:
        return None
    in_flight = conn.execute("SELECT count(*) FROM runs WHERE status = 'dispatched'").fetchone()[0]
    waiting = conn.execute("SELECT DISTINCT repo FROM runs WHERE status = 'queued'").fetchall()
    if in_flight >= limits.max_in_flight or all(r["repo"] in held for r in waiting):
        return None
    usage.refresh(conn, limits, now)
    if brakes.usage_hold(db_state.latest_usage(conn), limits, now) is not None:
        return None
    return limits.daily_cap - brakes.daily_count(conn, now)


def _try_later[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    """`f`, with GitHub or git trouble turned into `loop.TryLater`."""

    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return f(*args, **kwargs)
        except (GitHubError, OSError) as e:
            raise loop.TryLater(str(e)) from e

    return wrapped


def _bundle_check(conn: sqlite3.Connection, github: GitHub, settings: Settings) -> loop.Check:
    """The bundle checks of the runner contract, for every task's result."""
    store = bundles.Store(settings.git_dir, github.git_remote)

    @_try_later
    def check(row: sqlite3.Row, result: dict[str, Any], files: Any) -> None:
        dispatch = json.loads(row["dispatch"])
        enrolment = enrolments.get(conn, row["repo"])
        store.check(row["repo"], row["id"], files.bundle, task=row["task"],
                    verdict=(result.get("output") or {}).get("verdict"),
                    base_sha=dispatch.get("base_sha", ""),
                    pr_base_sha=dispatch.get("pr_base_sha"),
                    test_paths=(enrolment.config.test_paths or []) if enrolment else [],
                    findings_only=dispatch.get("findings_only") is True)

    return check


def handle_finished(
    conn: sqlite3.Connection, github: GitHub, runner: Runner, settings: Settings | None = None
) -> None:
    """Hand each done run to its workflow, once (see `factory.workflows`), which
    posts its result and queues what comes next.

    A row is marked handled only after its writes succeed, so a failed post
    is tried again on the next pass.
    """
    ctx = workflows.Context(conn, github, settings or Settings(), runner)
    rows = conn.execute(
        "SELECT * FROM runs WHERE status = 'done' AND handled_at IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        try:
            workflows.on_result(ctx, row)
        except (GitHubError, OSError) as e:
            log.error("run %s: writing the result to GitHub failed: %s", row["id"], e,
                      extra=about_row(row))
            continue
        with conn:
            conn.execute(
                "UPDATE runs SET handled_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(timespec="seconds"), row["id"]),
            )


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
    attempts.lift_holds(conn)
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
