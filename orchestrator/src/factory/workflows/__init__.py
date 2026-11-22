"""Workflows: the sequences of tasks the orchestrator chains (orchestrator.md#workflows).

Each workflow is a module behind one interface (`base.Workflow`): it receives
a finished run and decides what to queue next. Adding a workflow means adding
a module and listing it in `BY_KIND`.
"""

import sqlite3

from factory import attempts
from factory.workflows import base
from factory.workflows import implement_ticket
from factory.workflows.base import Context, Workflow

__all__ = ["BY_KIND", "Context", "Workflow", "on_result"]

BY_KIND: dict[str, Workflow] = {implement_ticket.KIND: implement_ticket}  # type: ignore[dict-item]


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
    BY_KIND[workflow["kind"]].on_result(ctx, run, workflow)
