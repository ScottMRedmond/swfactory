"""Workflows: the sequences of tasks the orchestrator chains (orchestrator.md#workflows).

Each workflow is a module behind one interface (`base.Workflow`): it receives
a finished run and decides what to queue next. Adding a workflow means adding
a module and listing it in `BY_KIND`.
"""

import sqlite3
from typing import Any

from factory.workflows import base
from factory.workflows import implement_spec, implement_ticket
from factory.workflows.base import Context, Workflow

__all__ = ["BY_KIND", "Context", "Workflow", "on_result", "prepare"]

BY_KIND: dict[str, Workflow] = {  # type: ignore[dict-item]
    implement_ticket.KIND: implement_ticket,
    implement_spec.KIND: implement_spec,
}


def on_result(ctx: Context, run: sqlite3.Row) -> None:
    """Hand a done run to its workflow. A run outside any workflow (e.g. smoke)
    needs nothing."""
    if run["workflow_id"] is None:
        return
    workflow = base.get(ctx.conn, run["workflow_id"])
    if workflow is None:
        return
    BY_KIND[workflow["kind"]].on_result(ctx, run, workflow)


def prepare(ctx: Context, run: sqlite3.Row) -> dict[str, Any] | None:
    """The dispatch to send for a queued run, freshened by its workflow just
    before it goes out (e.g. implement's base), or None to send it as queued."""
    if run["workflow_id"] is None:
        return None
    workflow = base.get(ctx.conn, run["workflow_id"])
    if workflow is None:
        return None
    return BY_KIND[workflow["kind"]].prepare(ctx, run, workflow)
