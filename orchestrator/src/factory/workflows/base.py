"""What every workflow shares: the interface, its `workflows` row and the status labels.

A workflow is a module with a `KIND` and an `on_result(ctx, run, workflow)`
that receives a finished run of the workflow and decides what to queue next
(orchestrator.md#workflows). Its row holds what belongs to the workflow rather
than to one run; `status` is `active`, `done`, `stopped` or `cancelled`.
"""

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from factory import bundles
from factory.github import GitHub
from factory.runner import Runner

if TYPE_CHECKING:
    from factory.orchestrator import Settings

# A workflow's status (the `workflows.status` column).
ACTIVE = "active"
DONE = "done"
STOPPED = "stopped"
CANCELLED = "cancelled"

IN_PROGRESS = "agent:in-progress"
# Status labels a run starting makes stale (orchestrator.md#chaining).
STALE = ("needs-a-human", "waiting", "agent:blocked")


class _NoRunner:
    """The runner where nothing may use it: starting, queueing and ending a
    workflow never do. Any use fails loudly."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"this Context has no runner (asked for {name})")


NO_RUNNER = cast(Runner, _NoRunner())


@dataclass(frozen=True)
class Context:
    """What a workflow acts with. Handling a run's result needs the runner;
    starting, queueing and ending a workflow leave it out."""

    conn: sqlite3.Connection
    github: GitHub
    settings: "Settings"
    runner: Runner = NO_RUNNER


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
            conn.execute("UPDATE workflows SET status = ? WHERE id = ?", (ACTIVE, row["id"]))
            return row["id"]
        return conn.execute(
            "INSERT INTO workflows (repo, kind, subject, status) VALUES (?, ?, ?, ?)",
            (repo, kind, subject, ACTIVE),
        ).lastrowid


def get(conn: sqlite3.Connection, workflow_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()


def of_pr(conn: sqlite3.Connection, repo: str, kind: str, pr: int) -> sqlite3.Row | None:
    """The newest workflow of `kind` whose PR is `pr`."""
    return conn.execute(
        "SELECT * FROM workflows WHERE repo = ? AND kind = ? AND pr = ? ORDER BY id DESC LIMIT 1",
        (repo, kind, pr)).fetchone()


def closed_unmerged(
    conn: sqlite3.Connection, github: GitHub, repo: str, issues: list[dict[str, Any]], *,
    kind: str, statuses: tuple[str, ...], full_scan: bool,
) -> list[sqlite3.Row]:
    """The workflows of `kind` in one of `statuses` whose PR a human closed
    without merging. `issues` is what the poll found, as for
    `orchestrator.cancel_closed`. A full scan only lists open issues and PRs,
    so it looks at workflows whose PR is missing from it but whose subject
    (ticket or spec) is there: a merged PR closes its subject."""
    rows = conn.execute(
        f"SELECT * FROM workflows WHERE repo = ? AND kind = ? AND pr IS NOT NULL "
        f"AND status IN ({', '.join('?' * len(statuses))}) ORDER BY id",
        (repo, kind, *statuses)).fetchall()
    if full_scan:
        open_numbers = {i["number"] for i in issues}
        maybe = [w for w in rows
                 if w["pr"] not in open_numbers and int(w["subject"]) in open_numbers]
    else:
        closed_prs = {i["number"] for i in issues
                      if "pull_request" in i and i.get("state") == "closed"}
        maybe = [w for w in rows if w["pr"] in closed_prs]
    closed = []
    for workflow in maybe:
        pull = github.pull(repo, workflow["pr"])
        if pull["state"] == "closed" and not pull.get("merged"):
            closed.append(workflow)
    return closed


def store(ctx: Context) -> bundles.Store:
    """The orchestrator's git store, reaching GitHub as the App."""
    return bundles.Store(ctx.settings.git_dir, ctx.github.git_remote)


def mark_in_progress(github: GitHub, repo: str, number: int) -> None:
    """A run is starting on the issue: stale status labels off, `agent:in-progress` on."""
    for label in STALE:
        github.remove_label(repo, number, label)
    github.add_labels(repo, number, [IN_PROGRESS])


def end(
    ctx: Context, workflow: sqlite3.Row, status: str, number: int, label: str | None = None
) -> None:
    """End the workflow with `status` (`DONE`, `STOPPED` or `CANCELLED`):
    `agent:in-progress` comes off issue `number`, and `label` (what stopped
    it) goes on."""
    if label is not None:
        ctx.github.add_labels(workflow["repo"], number, [label])
    ctx.github.remove_label(workflow["repo"], number, IN_PROGRESS)
    with ctx.conn:
        ctx.conn.execute("UPDATE workflows SET status = ? WHERE id = ?", (status, workflow["id"]))
