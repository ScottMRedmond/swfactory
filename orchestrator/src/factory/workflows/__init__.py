"""Workflows: the sequences of tasks the orchestrator chains (orchestrator.md#workflows).

Each workflow is a module behind one interface (`base.Workflow`): it receives
a finished run and decides what to queue next. Adding a workflow means adding
a module and listing it in `BY_KIND`.
"""

import sqlite3

from factory.workflows import base
from factory.workflows import implement_ticket
from factory.workflows.base import Context, Workflow

__all__ = ["BY_KIND", "Context", "Workflow", "on_result"]

BY_KIND: dict[str, Workflow] = {implement_ticket.KIND: implement_ticket}  # type: ignore[dict-item]


def on_result(ctx: Context, run: sqlite3.Row) -> None:
    """Hand a done run to its workflow. A run outside any workflow (e.g. smoke)
    needs nothing."""
    if run["workflow_id"] is None:
        return
    workflow = base.get(ctx.conn, run["workflow_id"])
    if workflow is None:
        return
    BY_KIND[workflow["kind"]].on_result(ctx, run, workflow)
