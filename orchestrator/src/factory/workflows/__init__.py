"""Workflows: the sequences of tasks the orchestrator chains (orchestrator.md#workflows).

Each workflow is a module behind one interface (`base.Workflow`): it receives
a finished run and decides what to queue next. Adding a workflow means adding
a module and listing it in `BY_KIND`.
"""

import logging
import sqlite3
from typing import Any

from factory import attempts, bundles
from factory.logs import about_run
from factory.tickets import render_notice
from factory.workflows import base
from factory.workflows import implement_spec, implement_ticket, review_pr
from factory.workflows.base import Context, Workflow

__all__ = ["BY_KIND", "Context", "Workflow", "on_result", "prepare"]

log = logging.getLogger(__name__)

BY_KIND: dict[str, Workflow] = {  # type: ignore[dict-item]
    implement_ticket.KIND: implement_ticket,
    implement_spec.KIND: implement_spec,
    review_pr.KIND: review_pr,
}


def on_result(ctx: Context, run: sqlite3.Row) -> None:
    """Hand a done run to its workflow, after the attempts rules: a failure
    that isn't an attempt is queued again, and a bad attempt that isn't the
    last is retried (`factory.attempts`). A run outside any workflow (e.g.
    smoke) needs nothing more."""
    not_an_attempt = attempts.not_an_attempt(ctx.conn, run)
    if run["workflow_id"] is None:
        return
    workflow = base.get(ctx.conn, run["workflow_id"])
    if workflow is None:
        return
    if not_an_attempt:
        if workflow["status"] == "active":
            attempts.requeue(ctx.conn, run, attempt=run["attempt"])
        return
    if workflow["status"] == "active" and attempts.is_bad(run) and attempts.retry(ctx.conn, run):
        return
    try:
        BY_KIND[workflow["kind"]].on_result(ctx, run, workflow)
    except bundles.Lost as e:
        _lost(ctx, run, workflow, e)


def _lost(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, why: bundles.Lost) -> None:
    """A run whose commits are gone for good is handled once: a notice saying
    how to run it again, and `needs-a-human` on its ticket (or PR). A spec's
    ticket stops alone; the spec's queue goes on without it. Any other
    workflow stops."""
    log.error("run %s: its commits are lost: %s", run["id"], why,
              extra=about_run(id=run["id"], repo=run["repo"], ticket=run["ticket"]))
    repo, number = run["repo"], int(run["ticket"])
    trigger = "agent:review" if run["task"] == "review" else "agent:implement"
    ctx.github.post_comment(repo, number, render_notice(
        f"Factory run {run['id']} finished, but its commits were lost: the factory's git store "
        "no longer has them and the runner's files have expired. Nothing was pushed. Add "
        f"`{trigger}` again to run it again."))
    if workflow["kind"] == implement_spec.KIND and run["task"] == "implement":
        ctx.github.add_labels(repo, number, ["needs-a-human"])
        ctx.github.remove_label(repo, number, base.IN_PROGRESS)
    elif workflow["status"] == "active":
        base.end(ctx, workflow, "stopped", number, "needs-a-human")


def prepare(ctx: Context, run: sqlite3.Row) -> dict[str, Any] | None:
    """The dispatch to send for a queued run, freshened by its workflow just
    before it goes out (e.g. implement's base), or None to send it as queued."""
    if run["workflow_id"] is None:
        return None
    workflow = base.get(ctx.conn, run["workflow_id"])
    if workflow is None:
        return None
    return BY_KIND[workflow["kind"]].prepare(ctx, run, workflow)
