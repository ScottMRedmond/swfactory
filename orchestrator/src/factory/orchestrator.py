"""The orchestrator's work on GitHub: finding triggers and posting results.

`run_once` is one pass of the main loop: poll each repo for trigger labels,
`loop.tick` the queue (collect, then dispatch), then act on finished runs.
`serve` runs it about every 60 seconds.

Today only `agent:explore` is handled, on repos named in `Settings.repos`.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from factory import loop, state
from factory.contract import CONTRACT_VERSION
from factory.github import GitHub, GitHubError
from factory.logs import about_run
from factory.runner import Runner

log = logging.getLogger(__name__)

EXPLORE_LABEL = "agent:explore"


@dataclass(frozen=True)
class Settings:
    repos: list[str]
    poll_seconds: int = 60
    max_in_flight: int = 2
    timeouts: dict[str, int] = field(default_factory=lambda: {"explore": 20})
    models: dict[str, str] = field(default_factory=lambda: {"explore": "claude-sonnet-4-6"})


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
    """
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
                    "enrolment_sha": head,
                    "model": settings.models["explore"],
                    "timeout_minutes": settings.timeouts["explore"],
                },
            )
            log.info("queued explore run %s for %s#%s", run_id, repo, ticket,
                     extra=about_run(id=run_id, repo=repo, ticket=ticket))
        github.remove_label(repo, issue["number"], EXPLORE_LABEL)


def _about(row: sqlite3.Row) -> dict[str, Any]:
    return about_run(id=row["id"], repo=row["repo"], ticket=row["ticket"])


def run_once(conn: sqlite3.Connection, github: GitHub, runner: Runner, settings: Settings) -> None:
    """One pass: poll every repo, tick the queue, act on finished runs.

    A repo whose calls fail is logged and skipped; the others carry on.
    """
    for repo in settings.repos:
        try:
            poll(conn, github, repo, settings)
            state.record_poll(conn, repo)
        except (GitHubError, OSError) as e:
            log.error("polling %s failed: %s", repo, e, extra={"repo": repo})
    loop.tick(conn, runner, limit=settings.max_in_flight)
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
) -> None:
    """Run `run_once` about every `poll_seconds`, until interrupted."""
    log.info("serving %s, polling every %ss", ", ".join(settings.repos), settings.poll_seconds)
    while True:
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
