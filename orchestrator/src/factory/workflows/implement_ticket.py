"""The implement-ticket workflow: explore -> implement -> review, for one ticket
and one PR (orchestrator.md#implement-ticket).

A human starts it with `agent:explore` (later tickets add joining it with
`agent:implement` or `agent:review`). Explore's verdict decides what happens
next (orchestrator.md#explore): `easy-call` queues implement; the others stop
the workflow. Implement starts from the default branch's head at dispatch
time; `built` pushes its checked bundle to `factory/<ticket>-<run id>` and
opens a draft PR, `needs-a-human` pushes nothing and stops the workflow
(orchestrator.md#implement).
"""

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from factory import bundles, loop, tickets
from factory import enrolment as enrolments
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
    cancel`) cancels the workflow. Otherwise explore's and implement's results
    are handled so far; review (#13) adds its own, and failed runs are #16's."""
    if run["outcome"] == "cancelled":
        if workflow["status"] == "active":
            base.end(ctx, workflow, "cancelled", int(run["ticket"]))
    elif run["task"] == "explore" and run["outcome"] == "succeeded":
        _explored(ctx, run, workflow, json.loads(run["result"])["output"])
    elif run["task"] == "implement" and run["outcome"] == "succeeded":
        _implemented(ctx, run, workflow, json.loads(run["result"])["output"])


def prepare(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> dict[str, Any] | None:
    """Implement starts from the default branch's head at dispatch time, for
    every attempt, so a row held back by the usage pause or a retry doesn't
    build on stale code (orchestrator.md#implement-ticket)."""
    if run["task"] != "implement":
        return None
    dispatch = json.loads(run["dispatch"])
    return {**dispatch, "base_sha": ctx.github.default_branch_head(run["repo"])}


def _implemented(
    ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, output: dict[str, Any]
) -> None:
    """Implement's verdict (orchestrator.md#implement). The result, including
    its bundle, was checked when it was collected."""
    repo, number = run["repo"], int(run["ticket"])
    link = ctx.runner.log_link(repo, run["runner_run_id"])
    if output["verdict"] == "needs-a-human":
        ctx.github.post_comment(repo, number,
                                render_needs_a_human_comment(run["id"], output["comment"], link))
        base.end(ctx, workflow, "stopped", number, "needs-a-human")
        log.info("implement run %s needs a human on %s#%s", run["id"], repo, number,
                 extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))
        return
    dispatch = json.loads(run["dispatch"])
    store = bundles.Store(ctx.settings.git_dir, ctx.github.git_remote)
    checked = _checked(ctx, store, run, dispatch["base_sha"])
    store.push(repo, run["id"], dispatch["branch"])
    issue = ctx.github.get_issue(repo, number)
    enrolment = enrolments.get(ctx.conn, repo)
    test_paths = enrolment.config.test_paths if enrolment else None
    notes = []
    if test_paths and not any(bundles.matches(f, test_paths) for f in checked.files):
        notes.append("⚠️ no test files changed")
    if _ticket_changed(ctx, run, dispatch, issue):
        notes.append("⚠️ the ticket changed after this run started")
    body = render_pr_body(number, run["id"], link, output.get("pr_body"), checked.log, notes)
    pr = ctx.github.open_draft_pull(repo, head=dispatch["branch"],
                                    base=ctx.github.default_branch(repo),
                                    title=issue["title"], body=body)
    with ctx.conn:
        ctx.conn.execute("UPDATE workflows SET pr = ? WHERE id = ?", (pr, workflow["id"]))
    log.info("implement run %s built %s#%s: draft PR #%s from %s", run["id"], repo, number, pr,
             dispatch["branch"], extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))


def _checked(
    ctx: Context, store: bundles.Store, run: sqlite3.Row, base_sha: str
) -> bundles.Checked:
    """The run's bundle as checked on collection. If the store lost it (e.g.
    it was wiped), the run's files are fetched and checked again."""
    checked = store.checked(run["repo"], run["id"], base_sha)
    if checked is None:
        files = ctx.runner.files(run["repo"], run["runner_run_id"])
        if files is None or files.bundle is None:
            raise bundles.GitError(f"run {run['id']}'s bundle is gone from the runner")
        checked = store.check(run["repo"], run["id"], files.bundle, task="implement",
                              verdict="built", base_sha=base_sha)
    assert checked is not None
    return checked


def _ticket_changed(
    ctx: Context, run: sqlite3.Row, dispatch: dict[str, Any], issue: dict[str, Any]
) -> bool:
    """Whether the ticket was edited, or got a comment from someone with write
    access, after the run's `ticket.md` was rendered."""
    ticket = tickets.fetch(ctx.github, run["repo"], issue)
    rendered = dispatch["ticket_markdown"]
    if rendered.startswith("> Trimmed"):
        rendered = rendered.split("\n", 2)[2]
    head = tickets.render(replace(ticket, comments=[], history=[]))
    if not rendered.startswith(head):
        return True
    rest = rendered[len(head):]
    if rest and not rest.startswith("\n## "):
        return True
    since = _when(run["created_at"])  # when its ticket.md was rendered
    app = ctx.github.app_login()
    return any(_when(c.created_at) > since for c in ticket.comments if c.author != app)


def _when(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


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


DRAFT_NOTICE = ("This PR stays a draft until the factory's review is posted. "
                "A human merges it.")


def render_pr_body(
    number: int, run_id: int, log_link: str, pr_body: str | None, log_lines: list[str],
    notes: list[str],
) -> str:
    """The draft PR's body: implement's `pr_body`, or the commit log without
    one, then the orchestrator's footer."""
    if pr_body and pr_body.strip():
        summary = pr_body.strip()
    else:
        summary = "Commits:\n\n```\n" + "\n".join(log_lines) + "\n```"
    footer = [f"Closes #{number}", *notes, f"Factory run {run_id} · [runner log]({log_link})",
              DRAFT_NOTICE]
    return f"{summary}\n\n---\n" + "\n\n".join(footer) + "\n"


def render_needs_a_human_comment(run_id: int, comment: str, log_link: str) -> str:
    """Implement's `needs-a-human` comment, under the factory's own heading."""
    return (f"<!-- factory:implement run={run_id} -->\n## Implement needs a human\n\n"
            f"{comment.strip()}\n\nNothing was pushed. Once the ticket is settled, add "
            f"`agent:implement` to build it.\n\n---\nFactory run {run_id} · "
            f"[runner log]({log_link})\n")


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
