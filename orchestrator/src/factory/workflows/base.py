"""What every workflow shares: the interface, its `workflows` row and the status labels.

A workflow is a module with a `KIND` and an `on_result(ctx, run, workflow)`
that receives a finished run of the workflow and decides what to queue next
(orchestrator.md#workflows). Its row holds what belongs to the workflow rather
than to one run; `status` is `active`, `done`, `stopped` or `cancelled`.
"""

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from factory.github import GitHub
from factory.runner import Runner

if TYPE_CHECKING:
    from factory.orchestrator import Settings

IN_PROGRESS = "agent:in-progress"
# Status labels a run starting makes stale (orchestrator.md#chaining).
STALE = ("needs-a-human", "waiting", "agent:blocked")


@dataclass(frozen=True)
class Context:
    """What a workflow acts with."""

    conn: sqlite3.Connection
    github: GitHub
    runner: Runner
    settings: "Settings"


class Workflow(Protocol):
    KIND: str

    def on_result(self, ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
        """Act on a done run of this workflow: post, label, queue the next task.
        Raises `GitHubError` or `OSError` to be called again on the next pass."""
        ...

    def prepare(
        self, ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row
    ) -> dict[str, Any] | None:
        """The dispatch to send for queued `run`, freshened just before it goes
        out, or None to send it as queued."""
        ...


def start(conn: sqlite3.Connection, *, repo: str, kind: str, subject: str) -> int:
    """The workflow for `subject` (a ticket or spec), made `active`. A subject
    has one workflow of each kind: a stopped (or ended) one is reused, so its
    history stays together."""
    with conn:
        row = conn.execute(
            "SELECT id FROM workflows WHERE repo = ? AND kind = ? AND subject = ? "
            "ORDER BY id DESC LIMIT 1",
            (repo, kind, subject),
        ).fetchone()
        if row is not None:
            conn.execute("UPDATE workflows SET status = 'active' WHERE id = ?", (row["id"],))
            return row["id"]
        return conn.execute(
            "INSERT INTO workflows (repo, kind, subject, status) VALUES (?, ?, ?, 'active')",
            (repo, kind, subject),
        ).lastrowid


def get(conn: sqlite3.Connection, workflow_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()


def mark_in_progress(github: GitHub, repo: str, number: int) -> None:
    """A run is starting on the issue: stale status labels off, `agent:in-progress` on."""
    for label in STALE:
        github.remove_label(repo, number, label)
    github.add_labels(repo, number, [IN_PROGRESS])


def end(
    ctx: Context, workflow: sqlite3.Row, status: str, number: int, label: str | None = None
) -> None:
    """End the workflow with `status`: `agent:in-progress` comes off issue
    `number`, and `label` (what stopped it) goes on."""
    if label is not None:
        ctx.github.add_labels(workflow["repo"], number, [label])
    ctx.github.remove_label(workflow["repo"], number, IN_PROGRESS)
    with ctx.conn:
        ctx.conn.execute("UPDATE workflows SET status = ? WHERE id = ?", (status, workflow["id"]))
