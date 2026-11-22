"""The implement-spec workflow: spec explore -> auto-tickets -> implement each
ticket -> one review, for a whole spec on one integration branch
(orchestrator.md#implement-spec).

So far it handles auto-tickets' result (orchestrator.md#auto-tickets); starting
the workflow and spec explore (#31), the build queue (#32), the spec PR (#33)
and stalls (#34) are added by their tickets.
"""

import json
import logging
import re
import sqlite3
from typing import Any

from factory.logs import about_run
from factory.tickets import render_notice
from factory.workflows import base
from factory.workflows.base import Context

log = logging.getLogger(__name__)

KIND = "implement-spec"

SPEC_LABEL_COLOR = "c5def5"
_MARKER = re.compile(r"<!-- factory:auto-tickets run=(\d+) index=(\d+) -->")


def spec_label(spec: int | str) -> str:
    return f"spec:{spec}"


def on_result(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Act on a done run of the workflow. A cancelled run cancels it."""
    if run["outcome"] == "cancelled":
        if workflow["status"] == "active":
            base.end(ctx, workflow, "cancelled", int(run["ticket"]))
    elif run["task"] == "auto-tickets" and run["outcome"] == "succeeded":
        output = json.loads(run["result"])["output"]
        if output["verdict"] == "decomposed":
            _decomposed(ctx, run, output["tickets"])
        else:
            _needs_a_human(ctx, run, workflow, output["comment"])


def _decomposed(ctx: Context, run: sqlite3.Row, tickets: list[dict[str, Any]]) -> None:
    """One issue per ticket, in order, then a notice on the spec.

    Crash-safe: each body carries the run and its index, so after a restart
    the tickets this run already made are found (open or closed) and skipped.
    Blockers come first (checked when the result was collected, see
    `loop._judge`), so every `blocked_by` index has its issue number by the
    time a ticket names it.
    """
    repo, spec = run["repo"], int(run["ticket"])
    label = spec_label(spec)
    ctx.github.create_label(repo, label, SPEC_LABEL_COLOR, f"A ticket of spec #{spec}")
    numbers = made_by(ctx.github.issues_with_label(repo, label), run["id"])
    for index, ticket in enumerate(tickets):
        if index in numbers:
            continue
        blockers = [numbers[i] for i in ticket["blocked_by"]]
        body = render_ticket(run["id"], index, spec, ticket, blockers)
        numbers[index] = ctx.github.create_issue(repo, ticket["title"], body, [label])
        log.info("auto-tickets run %s: created %s#%s (ticket %s of %s)", run["id"], repo,
                 numbers[index], index + 1, len(tickets), extra=_about(run))
    listed = "\n".join(f"{i + 1}. #{numbers[i]}" for i in range(len(tickets)))
    ctx.github.post_comment(repo, spec, render_notice(
        f"Auto-tickets (factory run {run['id']}) broke this spec into {len(tickets)} "
        f"ticket{'s' if len(tickets) != 1 else ''}, labelled `{label}`:\n\n{listed}\n\n"
        "They're built in order on the spec's integration branch. Edit them at any time: "
        "the queue re-reads them on every poll."))


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
