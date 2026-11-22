"""The implement-ticket workflow: explore -> implement -> review, for one ticket
and one PR (orchestrator.md#implement-ticket).

A human starts it with `agent:explore` (later tickets add joining it with
`agent:implement` or `agent:review`). Explore's verdict decides what happens
next (orchestrator.md#explore): `easy-call` queues implement; the others stop
the workflow.
"""

import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from factory import enrolment as enrolments
from factory import loop, tickets
from factory.contract import CONTRACT_VERSION
from factory.enrolment import Enrolment
from factory.github import GitHub
from factory.logs import about_run
from factory.workflows import base
from factory.workflows.base import Context

if TYPE_CHECKING:
    from factory.orchestrator import Settings
    from factory.triggers import Added

log = logging.getLogger(__name__)

KIND = "implement-ticket"


def start_explore(
    conn: sqlite3.Connection,
    github: GitHub,
    settings: "Settings",
    issue: dict[str, Any],
    enrolment: Enrolment,
    triggered_by: "Added | None",
) -> int | None:
    """Start (or restart) the ticket's workflow with an explore run: the row is
    queued and the ticket marked in progress. Returns the run id, or None if the ticket is too
    big to dispatch, when a notice says so and nothing starts."""
    repo, ticket = enrolment.repo, str(issue["number"])
    login = triggered_by.login if triggered_by else None
    try:
        dispatch = tickets.fit(
            {
                "contract_version": CONTRACT_VERSION,
                "repo": repo,
                "task": "explore",
                "ticket": ticket,
                "older_issues": tickets.older_issues(github, repo, issue["number"]),
                "base_sha": github.default_branch_head(repo),
                "enrolment_sha": enrolment.sha,
                "model": settings.model("explore", enrolment),
                "timeout_minutes": settings.timeout("explore", enrolment),
            },
            tickets.fetch(github, repo, issue),
        )
    except tickets.TooBig as e:
        github.post_comment(repo, issue["number"], tickets.render_too_big_notice(str(e), login))
        log.info("%s#%s is too big to dispatch: %s", repo, ticket, e)
        return None
    workflow_id = base.start(conn, repo=repo, kind=KIND, subject=ticket)
    run_id = loop.enqueue(conn, repo=repo, task="explore", ticket=ticket, dispatch=dispatch,
                          workflow_id=workflow_id, triggered_by=login,
                          triggered_by_id=triggered_by.id if triggered_by else None)
    log.info("queued explore run %s for %s#%s", run_id, repo, ticket,
             extra=about_run(id=run_id, repo=repo, ticket=ticket))
    base.mark_in_progress(github, repo, issue["number"])
    return run_id


def on_result(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Act on a done run. A cancelled run (its ticket closed, or `factory
    cancel`) cancels the workflow. Otherwise only explore's results are
    handled so far; implement (#12) and review (#13) add theirs, and failed
    runs are #16's."""
    if run["outcome"] == "cancelled":
        if workflow["status"] == "active":
            base.end(ctx, workflow, "cancelled", int(run["ticket"]))
    elif run["task"] == "explore" and run["outcome"] == "succeeded":
        _explored(ctx, run, workflow, json.loads(run["result"])["output"])


def _explored(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, output: dict[str, Any]) -> None:
    """Explore's verdict (orchestrator.md#explore)."""
    repo, number = run["repo"], int(run["ticket"])
    verdict = output["verdict"]

    def post(notes: list[str] | None = None) -> None:
        link = ctx.runner.log_link(repo, run["runner_run_id"])
        ctx.github.post_comment(repo, number, render_explore_comment(run["id"], output, link, notes))
        log.info("posted explore findings of run %s on %s#%s (%s)", run["id"], repo, number,
                 verdict, extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))

    if verdict in ("needs-a-human", "waiting"):
        post()
        base.end(ctx, workflow, "stopped", number, verdict)
    elif verdict == "easy-call":
        issue = ctx.github.get_issue(repo, number)
        why_not = _why_not_implement(ctx, run, issue)
        if why_not is not None:
            post([why_not])
            base.end(ctx, workflow, "stopped", number, "needs-a-human")
            return
        post()
        _queue_implement(ctx, run, issue)
    elif verdict == "duplicate":
        duplicate_of = output["duplicate_of"]
        if ctx.github.get_issue(repo, int(duplicate_of))["state"] != "open":
            post([f"#{duplicate_of} has been closed since, so this ticket was left open: only a "
                  "human can tell whether its fix covers this one too."])
            base.end(ctx, workflow, "stopped", number, "needs-a-human")
            return
        # The findings first: a reopened duplicate's history is read from the
        # App's explore comment ("### Duplicate of") before the App's close.
        post()
        ctx.github.close_as_duplicate(repo, number)
        base.end(ctx, workflow, "stopped", number)
    elif verdict == "too-big":
        post(["Add `agent:implement-spec` to build this as a spec."])
        base.end(ctx, workflow, "stopped", number, "needs-a-human")


def _why_not_implement(ctx: Context, run: sqlite3.Row, issue: dict[str, Any]) -> str | None:
    """Why an `easy-call` can't go on to implement by itself, or None if it can.

    An outsider's ticket waits for someone with write access to read the plan
    first, so code is never written from an outsider's text unread.
    """
    enrolment = enrolments.get(ctx.conn, run["repo"])
    if enrolment is None or "implement" not in enrolment.config.enabled_tasks():
        return ("`implement` isn't enabled for this repo (see `tasks` in `.factory/config.yml`), "
                "so nothing is built.")
    author = issue["user"]["login"]
    if ctx.github.permission(run["repo"], author) not in tickets.WRITERS:
        return (f"@{author}, who opened this ticket, doesn't have write access, so nothing is "
                "built until someone who does reads the plan and adds `agent:implement`.")
    if run["triggered_by"] is None or run["triggered_by_id"] is None:
        # Implement's commits credit whoever triggered the work; here no one is known.
        return ("The issue's events don't say who added `agent:explore`. Nothing is built "
                "until someone with write access adds `agent:implement`.")
    return None


def _queue_implement(ctx: Context, after: sqlite3.Row, issue: dict[str, Any]) -> None:
    """Queue implement for the ticket, chained from run `after` (whose
    `triggered_by` it copies). Nothing if `after` already queued one: a crash
    between queueing and marking `after` handled must not queue it twice."""
    if ctx.conn.execute(
        "SELECT 1 FROM runs WHERE workflow_id = ? AND task = 'implement' AND id > ?",
        (after["workflow_id"], after["id"]),
    ).fetchone():
        return
    repo, ticket = after["repo"], after["ticket"]
    enrolment = enrolments.get(ctx.conn, repo)
    assert enrolment is not None  # an unenrolled repo's runs are never handled
    dispatch = tickets.fit(
        {
            "contract_version": CONTRACT_VERSION,
            "repo": repo,
            "task": "implement",
            "ticket": ticket,
            "base_sha": ctx.github.default_branch_head(repo),
            # Sized with the longest run id; `enqueue` names it with the real one.
            "branch": _branch(ticket, tickets.RUN_ID_ROOM),
            "triggered_by": {"login": after["triggered_by"], "id": after["triggered_by_id"]},
            "enrolment_sha": enrolment.sha,
            "model": ctx.settings.model("implement", enrolment),
            "timeout_minutes": ctx.settings.timeout("implement", enrolment),
        },
        tickets.fetch(ctx.github, repo, issue),
    )
    run_id = loop.enqueue(ctx.conn, repo=repo, task="implement", ticket=ticket, dispatch=dispatch,
                          workflow_id=after["workflow_id"], triggered_by=after["triggered_by"],
                          triggered_by_id=after["triggered_by_id"],
                          branch=lambda run_id: _branch(ticket, run_id))
    log.info("queued implement run %s for %s#%s after explore run %s", run_id, repo, ticket,
             after["id"], extra=about_run(id=run_id, repo=repo, ticket=ticket))


def _branch(ticket: str, run_id: int) -> str:
    return f"factory/{ticket}-{run_id}"


def render_explore_comment(
    run_id: int, output: dict[str, Any], log_link: str, notes: list[str] | None = None
) -> str:
    """Explore's findings under the factory's own headings, the same every time.
    `notes` are the orchestrator's own lines, put above the footer."""

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
    parts.extend(f"{note}\n" for note in notes or [])
    parts.append(f"---\nFactory run {run_id} · [runner log]({log_link})\n")
    return "\n".join(parts)
