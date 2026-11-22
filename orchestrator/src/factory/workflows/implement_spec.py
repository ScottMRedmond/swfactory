"""The implement-spec workflow: spec explore -> auto-tickets -> implement each
ticket -> one review, for a whole spec on one integration branch
(orchestrator.md#implement-spec).

A human starts it with `agent:implement-spec` (`start`): the issue is
labelled `spec` and spec explore runs on it, unless the spec still has open
`spec:<n>` tickets or a leftover integration branch. Spec explore's
`easy-call` posts the spec exploration notes and queues auto-tickets with
them; its other verdicts stop the workflow as for a ticket. Auto-tickets'
`decomposed` creates the tickets (orchestrator.md#auto-tickets), then the
integration branch `factory/spec-<n>` at auto-tickets' `base_sha`, and stores
the spec body's hash. `spec_ticket` renders a spec ticket with the notes.
The build queue (#32), the spec PR (#33) and stalls (#34) are added by their
tickets.
"""

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from factory import enrolment as enrolments
from factory import attempts, bundles, loop, tickets
from factory.contract import CONTRACT_VERSION
from factory.enrolment import Enrolment
from factory.github import GitHub
from factory.logs import about_run
from factory.tickets import render_notice
from factory.workflows import base, implement_ticket
from factory.workflows.base import Context
from factory.workflows.implement_ticket import render_explore_sections

if TYPE_CHECKING:
    from factory.orchestrator import Settings
    from factory.triggers import Added

log = logging.getLogger(__name__)

KIND = "implement-spec"
TRIGGER = "agent:implement-spec"
SPEC = "spec"  # the label on a spec issue
NOTES_HEADING = "Spec exploration notes"
EXPLORE_HEADING = "Spec explore findings"  # when spec explore stops the workflow

SPEC_LABEL_COLOR = "c5def5"
_MARKER = re.compile(r"<!-- factory:auto-tickets run=(\d+) index=(\d+) -->")


def spec_label(spec: int | str) -> str:
    return f"spec:{spec}"


def start(
    conn: sqlite3.Connection,
    github: GitHub,
    settings: "Settings",
    issue: dict[str, Any],
    enrolment: Enrolment,
    triggered_by: "Added | None",
) -> int | None:
    """A human added `agent:implement-spec` to a spec: label it `spec` and
    start the workflow with spec explore. Returns the explore run's id, or
    None when a notice says why nothing was queued."""
    repo, number = enrolment.repo, issue["number"]
    if triggered_by is None or triggered_by.id is None:
        # The tickets' commits credit whoever triggered the work; here no one is known.
        github.post_comment(repo, number, render_notice(
            f"The issue's events don't say who added `{TRIGGER}`, so nothing was started. "
            "Add it again."))
        return None
    login = triggered_by.login
    github.add_labels(repo, number, [SPEC])
    if _refuse_open_tickets(github, repo, number, login):
        return None
    branch = integration_branch(number)
    if bundles.Store(settings.git_dir, github.git_remote).branch_head(repo, branch):
        github.post_comment(repo, number, render_notice(
            f"The integration branch `{branch}` already exists, from an earlier start, so "
            "nothing was started (the factory never force-pushes). Delete or rename it, then "
            "add `agent:implement-spec` again.", [login] if login else None))
        log.info("%s#%s: %s already exists; implement-spec not started", repo, number, branch,
                 extra={"repo": repo, "ticket": str(number)})
        return None
    return implement_ticket.start_explore(conn, github, settings, issue, enrolment,
                                          triggered_by, kind=KIND)


def _refuse_open_tickets(github: GitHub, repo: str, spec: int, mention: str | None) -> bool:
    """If the spec already has open `spec:<n>` tickets, a notice says to close
    them first, and True: auto-tickets would make a second set."""
    label = spec_label(spec)
    numbers = sorted(i["number"] for i in github.open_issues_with_label(repo, label)
                     if "pull_request" not in i)
    if not numbers:
        return False
    listed = ", ".join(f"#{n}" for n in numbers)
    github.post_comment(repo, spec, render_notice(
        f"This spec already has open tickets labelled `{label}` ({listed}), so nothing was "
        "started: auto-tickets would make a second set. To start over, close them and add "
        "`agent:implement-spec` again.", [mention] if mention else None))
    log.info("%s#%s already has open tickets %s: implement-spec not started", repo, spec,
             listed, extra={"repo": repo, "ticket": str(spec)})
    return True


def on_result(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Act on a done run of the workflow. A cancelled run cancels it."""
    if run["outcome"] == "cancelled":
        if workflow["status"] == "active":
            base.end(ctx, workflow, "cancelled", int(run["ticket"]))
    elif attempts.is_bad(run):
        # The last attempt: `factory.attempts` retried the others.
        if workflow["status"] == "active":
            implement_ticket.give_up(ctx, run, workflow, again=TRIGGER)
    elif run["task"] == "explore" and run["outcome"] == "succeeded":
        _explored(ctx, run, workflow, json.loads(run["result"])["output"])
    elif run["task"] == "auto-tickets" and run["outcome"] == "succeeded":
        output = json.loads(run["result"])["output"]
        if output["verdict"] == "decomposed":
            _decomposed(ctx, run, workflow, output["tickets"])
        else:
            _needs_a_human(ctx, run, workflow, output["comment"])


def _explored(
    ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, output: dict[str, Any]
) -> None:
    """Spec explore's verdict: `needs-a-human`, `waiting` and `duplicate` stop
    the workflow as for a ticket. Otherwise (`easy-call`, or `too-big`, which
    doesn't apply: auto-tickets' ticket limit handles size) the findings are
    posted as the spec exploration notes and auto-tickets is queued."""
    if implement_ticket.explore_stopped(ctx, run, workflow, output, heading=EXPLORE_HEADING):
        return
    if ctx.conn.execute(
        "SELECT 1 FROM runs WHERE workflow_id = ? AND task = 'auto-tickets' AND id > ?",
        (run["workflow_id"], run["id"]),
    ).fetchone():
        return  # handled already, up to marking the run handled
    implement_ticket.post_explore(ctx, run, output, heading=NOTES_HEADING)
    spec = int(run["ticket"])
    if _refuse_open_tickets(ctx.github, run["repo"], spec, run["triggered_by"]):
        base.end(ctx, workflow, "stopped", spec)
        return
    _queue_auto_tickets(ctx, run, workflow)


def _queue_auto_tickets(ctx: Context, after: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Queue auto-tickets on the spec, with the notes of spec explore run
    `after` (whose `triggered_by` it copies) in its input."""
    repo, spec = after["repo"], int(after["ticket"])
    enrolment = enrolments.get(ctx.conn, repo)
    assert enrolment is not None  # an unenrolled repo's runs are never handled
    issue = ctx.github.get_issue(repo, spec)
    try:
        dispatch = tickets.fit(
            {
                "contract_version": CONTRACT_VERSION,
                "repo": repo,
                "task": "auto-tickets",
                "ticket": str(spec),
                "older_issues": tickets.older_issues(ctx.github, repo, spec),
                "base_sha": ctx.github.default_branch_head(repo),
                "enrolment_sha": enrolment.sha,
                "model": ctx.settings.model("auto-tickets", enrolment),
                "timeout_minutes": ctx.settings.timeout("auto-tickets", enrolment),
            },
            _with_notes(tickets.fetch(ctx.github, repo, issue), after),
        )
    except tickets.TooBig as e:
        ctx.github.post_comment(repo, spec,
                                tickets.render_too_big_notice(str(e), after["triggered_by"]))
        base.end(ctx, workflow, "stopped", spec)
        log.info("%s#%s is too big to dispatch: %s", repo, spec, e, extra=_about(after))
        return
    run_id = loop.enqueue(ctx.conn, repo=repo, task="auto-tickets", ticket=str(spec),
                          dispatch=dispatch, workflow_id=workflow["id"],
                          triggered_by=after["triggered_by"],
                          triggered_by_id=after["triggered_by_id"])
    log.info("queued auto-tickets run %s for %s#%s after spec explore run %s", run_id, repo,
             spec, after["id"], extra=about_run(id=run_id, repo=repo, ticket=str(spec)))


def spec_ticket(
    conn: sqlite3.Connection, github: GitHub, repo: str, issue: dict[str, Any],
    workflow: sqlite3.Row,
) -> tickets.Ticket:
    """A spec ticket (`issue`, as GitHub gives it) as its implement reads it:
    the ticket, then the spec exploration notes of `workflow`'s spec, so each
    implementer starts from them. Without notes, just the ticket."""
    ticket = tickets.fetch(github, repo, issue)
    explore = conn.execute(
        "SELECT * FROM runs WHERE workflow_id = ? AND task = 'explore' AND outcome = 'succeeded' "
        "ORDER BY id DESC LIMIT 1", (workflow["id"],)).fetchone()
    return ticket if explore is None else _with_notes(ticket, explore)


def _with_notes(ticket: tickets.Ticket, explore: sqlite3.Row) -> tickets.Ticket:
    """The spec as auto-tickets reads it: the spec exploration notes in their
    own section, and not again as the comment they were posted in."""
    mark = f"<!-- factory:explore run={explore['id']} -->"
    output = json.loads(explore["result"])["output"]
    return replace(ticket, comments=[c for c in ticket.comments if mark not in c.body],
                   sections=[*ticket.sections, (NOTES_HEADING, render_explore_sections(output))])


def prepare(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> dict[str, Any] | None:
    """Spec explore and auto-tickets read the default branch's head at
    dispatch time, so a row held back by the usage pause or a retry doesn't
    read stale code; the integration branch starts at auto-tickets' `base_sha`
    as sent. (A spec ticket's implement will start from the integration
    branch's head.)"""
    if run["task"] in ("explore", "auto-tickets"):
        dispatch = json.loads(run["dispatch"])
        return {**dispatch, "base_sha": ctx.github.default_branch_head(run["repo"])}
    return None


def integration_branch(spec: int | str) -> str:
    return f"factory/spec-{spec}"


def body_hash(body: str | None) -> str:
    """The `spec_hash` of a spec's body: an edit changes it, labels and
    comments don't (orchestrator.md#implement-spec)."""
    return hashlib.sha256((body or "").encode()).hexdigest()


def _decomposed(
    ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, breakdown: list[dict[str, Any]]
) -> None:
    """One issue per ticket, in order, then the integration branch, then a
    notice on the spec.

    Crash-safe: each body carries the run and its index, so after a restart
    the tickets this run already made are found (open or closed) and skipped.
    Blockers come first (checked when the result was collected, see
    `loop._judge`), so every `blocked_by` index has its issue number by the
    time a ticket names it. Creating the branch again is a no-op.
    """
    repo, spec = run["repo"], int(run["ticket"])
    label = spec_label(spec)
    ctx.github.create_label(repo, label, SPEC_LABEL_COLOR, f"A ticket of spec #{spec}")
    numbers = made_by(ctx.github.issues_with_label(repo, label), run["id"])
    for index, ticket in enumerate(breakdown):
        if index in numbers:
            continue
        blockers = [numbers[i] for i in ticket["blocked_by"]]
        body = render_ticket(run["id"], index, spec, ticket, blockers)
        numbers[index] = ctx.github.create_issue(repo, ticket["title"], body, [label])
        log.info("auto-tickets run %s: created %s#%s (ticket %s of %s)", run["id"], repo,
                 numbers[index], index + 1, len(breakdown), extra=_about(run))
    if not _create_integration_branch(ctx, run, workflow):
        return
    listed = "\n".join(f"{i + 1}. #{numbers[i]}" for i in range(len(breakdown)))
    ctx.github.post_comment(repo, spec, render_notice(
        f"Auto-tickets (factory run {run['id']}) broke this spec into {len(breakdown)} "
        f"ticket{'s' if len(breakdown) != 1 else ''}, labelled `{label}`:\n\n{listed}\n\n"
        f"They're built in order on the spec's integration branch, "
        f"`{integration_branch(spec)}`. Edit them at any time: "
        "the queue re-reads them on every poll."))


def _create_integration_branch(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> bool:
    """Create `factory/spec-<n>` at auto-tickets' `base_sha` (the code it read
    to write the breakdown) and store it, with the spec body's hash, in the
    workflow's row. If the branch is already somewhere else, nothing is
    forced: a notice asks a human to move it out of the way, the workflow
    stops, and False."""
    repo, spec = run["repo"], int(run["ticket"])
    branch = integration_branch(spec)
    base_sha = json.loads(run["dispatch"])["base_sha"]
    try:
        bundles.Store(ctx.settings.git_dir, ctx.github.git_remote).create_branch(
            repo, branch, base_sha)
    except bundles.Moved:
        ctx.github.post_comment(repo, spec, render_notice(
            f"The integration branch `{branch}` already exists, from an earlier start, so "
            "nothing is built (the factory never force-pushes). Delete or rename it, close "
            f"the `{spec_label(spec)}` tickets, and add `agent:implement-spec` again.",
            [run["triggered_by"]] if run["triggered_by"] else None))
        base.end(ctx, workflow, "stopped", spec, "needs-a-human")
        log.info("%s#%s: %s already exists elsewhere; workflow stopped", repo, spec, branch,
                 extra=_about(run))
        return False
    spec_hash = body_hash(ctx.github.get_issue(repo, spec).get("body"))
    with ctx.conn:
        ctx.conn.execute("UPDATE workflows SET branch = ?, spec_hash = ? WHERE id = ?",
                         (branch, spec_hash, workflow["id"]))
    log.info("auto-tickets run %s: created %s at %s for %s#%s", run["id"], branch, base_sha,
             repo, spec, extra=_about(run))
    return True


def _needs_a_human(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, comment: str) -> None:
    """No tickets: the comment goes on the spec, which is labelled `needs-a-human`."""
    repo, spec = run["repo"], int(run["ticket"])
    link = ctx.runner.log_link(repo, run["runner_run_id"])
    ctx.github.post_comment(repo, spec, (
        f"<!-- factory:auto-tickets run={run['id']} -->\n## Auto-tickets\n\n"
        f"**Verdict:** `needs-a-human`\n\n{comment.strip()}\n\n"
        f"---\nFactory run {run['id']} · [runner log]({link})\n"))
    base.end(ctx, workflow, "stopped", spec, "needs-a-human")
    log.info("auto-tickets run %s: %s#%s needs a human", run["id"], repo, spec, extra=_about(run))


def made_by(issues: list[dict[str, Any]], run_id: int) -> dict[int, int]:
    """The tickets auto-tickets run `run_id` has already created: index -> issue number."""
    found: dict[int, int] = {}
    for issue in issues:
        for run, index in _MARKER.findall(issue.get("body") or ""):
            if int(run) == run_id:
                found.setdefault(int(index), issue["number"])
    return found


def render_ticket(
    run_id: int, index: int, spec: int, ticket: dict[str, Any], blockers: list[int]
) -> str:
    """A ticket's issue body, in /to-tickets' template. Blockers are plain issue
    references, never GitHub's blocking links; the checkboxes are never ticked."""
    related = "".join(f"\n\nRelated: #{m}" for m in ticket.get("related", []))
    criteria = "\n".join(f"- [ ] {c}" for c in ticket["acceptance_criteria"])
    blocked_by = "\n".join(f"- #{n}" for n in blockers) or "None (can start immediately)"
    return (
        f"<!-- factory:auto-tickets run={run_id} index={index} -->\n"
        f"## Parent\n\n#{spec}\n\n"
        f"## What to build\n\n{ticket['what_to_build'].strip()}{related}\n\n"
        f"## Acceptance criteria\n\n{criteria}\n\n"
        f"## Blocked by\n\n{blocked_by}\n"
    )


def _about(run: sqlite3.Row) -> dict[str, Any]:
    return about_run(id=run["id"], repo=run["repo"], ticket=run["ticket"])
