"""The orchestrator's work on GitHub: finding triggers and posting results.

`run_once` is one pass of the main loop: poll each repo for trigger labels,
`loop.tick` the queue (collect, then dispatch), then act on finished runs.
`serve` runs it about every 60 seconds, and re-reads the enrolments (which
repos are polled, see `factory.enrolment`) every 10 minutes.

Today only `agent:explore` is handled.
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
from factory import loop
from factory.contract import CONTRACT_VERSION
from factory.enrolment import Enrolment
from factory.github import GitHub, GitHubError
from factory.runner import Runner

log = logging.getLogger(__name__)

EXPLORE_LABEL = "agent:explore"


@dataclass(frozen=True)
class Settings:
    """The global config. An enrolment's `timeouts` and `models` override the
    per-task defaults for its repo; the rest stays global."""

    # The factory's own repo, whose main holds factory-config.yml (`owner`).
    factory_repo: str = "ScottMRedmond/swfactory"
    poll_seconds: int = 60
    refresh_seconds: int = 600  # how often installations and enrolments are re-read
    max_in_flight: int = 2
    timeouts: dict[str, int] = field(default_factory=lambda: {
        "explore": 20, "auto-tickets": 20, "implement": 45, "review": 45})
    models: dict[str, str] = field(default_factory=lambda: {"explore": "claude-sonnet-4-6"})

    def timeout(self, task: str, enrolment: Enrolment) -> int:
        return enrolment.config.timeouts.get(task, self.timeouts[task])

    def model(self, task: str, enrolment: Enrolment) -> str:
        return enrolment.config.models.get(task, self.models[task])


def render_ticket(issue: dict[str, Any]) -> str:
    """The issue as `ticket.md`: its title and body."""
    body = (issue.get("body") or "").strip()
    markdown = f"# {issue['title']}\n"
    if body:
        markdown += f"\n{body}\n"
    return markdown


def poll(conn: sqlite3.Connection, github: GitHub, repo: str, settings: Settings) -> None:
    """Queue an explore run for each open issue labelled `agent:explore`.

    The row is inserted first, then the label removed, so a crash between the
    two leaves a duplicate trigger rather than a lost one. A ticket that
    already has a queued or dispatched explore isn't queued again.

    Only an enrolled repo is polled. The first poll of a repo creates the
    factory labels it lacks.
    """
    enrolment = enrolments.get(conn, repo)
    if enrolment is None:
        raise LookupError(f"{repo} isn't enrolled")
    enrolments.ensure_labels(conn, github, repo)
    for issue in github.open_issues_with_label(repo, EXPLORE_LABEL):
        if "pull_request" in issue:
            continue
        ticket = str(issue["number"])
        if not _active(conn, repo, "explore", ticket):
            head = github.default_branch_head(repo)
            run_id = loop.enqueue(
                conn,
                repo=repo,
                task="explore",
                ticket=ticket,
                dispatch={
                    "contract_version": CONTRACT_VERSION,
                    "repo": repo,
                    "task": "explore",
                    "ticket": ticket,
                    "ticket_markdown": render_ticket(issue),
                    "older_issues": [],
                    "base_sha": head,
                    "enrolment_sha": enrolment.sha,
                    "model": settings.model("explore", enrolment),
                    "timeout_minutes": settings.timeout("explore", enrolment),
                },
            )
            log.info("queued explore run %s for %s#%s", run_id, repo, ticket)
        github.remove_label(repo, issue["number"], EXPLORE_LABEL)


def run_once(conn: sqlite3.Connection, github: GitHub, runner: Runner, settings: Settings) -> None:
    """One pass: poll every enrolled repo, tick the queue, act on finished runs.

    A repo whose calls fail is logged and skipped; the others carry on.
    Queued runs of a repo with `enabled: false` (or no longer enrolled) wait.
    """
    for enrolment in enrolments.polled(conn):
        try:
            poll(conn, github, enrolment.repo, settings)
        except (GitHubError, OSError) as e:
            log.error("polling %s failed: %s", enrolment.repo, e)
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
                         row["id"], row["repo"], row["ticket"])
        except (GitHubError, OSError) as e:
            log.error("run %s: writing the result to GitHub failed: %s", row["id"], e)
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
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run `run_once` about every `poll_seconds`, until interrupted.

    Enrolments (and the factory's `owner`) are re-read at the start and then
    every `refresh_seconds`. A failed refresh is logged, the last known
    enrolments are used, and it's tried again on the next pass.
    """
    log.info("serving, polling every %ss", settings.poll_seconds)
    refreshed_at: float | None = None
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
            run_once(conn, github, runner, settings)
        except Exception:
            log.exception("the loop failed; trying again next poll")
        sleep(settings.poll_seconds)


def _active(conn: sqlite3.Connection, repo: str, task: str, ticket: str) -> bool:
    return (
        conn.execute(
            """SELECT 1 FROM runs WHERE repo = ? AND task = ? AND ticket = ?
               AND status IN ('queued', 'dispatched')""",
            (repo, task, ticket),
        ).fetchone()
        is not None
    )
