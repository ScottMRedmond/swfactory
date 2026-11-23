"""Workflows: the sequences of tasks the orchestrator chains (orchestrator.md#workflows).

Each workflow is a module behind one interface (`base.Workflow`): it receives
a finished run and decides what to queue next. Adding a workflow means adding
a module and listing it in `BY_KIND`, and routing the trigger label that starts
it in `orchestrator.handle_triggers` (which label starts which workflow, and
`implement` on a spec's ticket joining its spec, is read there in one place).

A module may also define these optional hooks, which the orchestrator calls
on every workflow that has them, through this module:

- `advance(ctx)`: each pass, before dispatching (`advance`).
- `cancel_closed_prs(conn, github, settings, repo, issues, *, full_scan)`:
  each poll, for PRs closed without merging (`cancel_closed_prs`).
- `start_review(conn, github, settings, pr, enrolment, triggered_by) -> bool`:
  `agent:review` on a PR; True if the PR is the workflow's and it acted
  (`start_review`).
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from factory import attempts, bundles
from factory.enrolment import Enrolment
from factory.github import GitHub
from factory.logs import about_row
from factory.tickets import render_notice
from factory.workflows import base
from factory.workflows import implement_spec, implement_ticket, review_pr
from factory.workflows.base import Context, Workflow

if TYPE_CHECKING:
    from factory.orchestrator import Settings
    from factory.triggers import Added

__all__ = ["BY_KIND", "Context", "Workflow", "advance", "cancel_closed_prs", "on_result",
           "prepare", "start_review"]

log = logging.getLogger(__name__)

BY_KIND: dict[str, Workflow] = {  # type: ignore[dict-item]
    implement_ticket.KIND: implement_ticket,
    implement_spec.KIND: implement_spec,
    review_pr.KIND: review_pr,
}


def _hooks(name: str) -> list[Callable[..., Any]]:
    return [getattr(m, name) for m in BY_KIND.values() if hasattr(m, name)]


def advance(ctx: Context) -> None:
    """Each pass, every workflow's `advance` hook (e.g. a spec's build queue)."""
    for hook in _hooks("advance"):
        hook(ctx)


def cancel_closed_prs(
    conn: sqlite3.Connection, github: GitHub, settings: "Settings", repo: str,
    issues: list[dict[str, Any]], *, full_scan: bool,
) -> None:
    """Each poll, every workflow ends those of its own whose PR a human closed
    without merging."""
    for hook in _hooks("cancel_closed_prs"):
        hook(conn, github, settings, repo, issues, full_scan=full_scan)


def start_review(
    conn: sqlite3.Connection, github: GitHub, settings: "Settings", pr: dict[str, Any],
    enrolment: Enrolment, triggered_by: "Added | None",
) -> None:
    """`agent:review` on a (non-fork) PR: the workflow that owns the PR
    reviews it again; a PR no workflow owns gets a review-pr workflow."""
    for hook in _hooks("start_review"):
        if hook(conn, github, settings, pr, enrolment, triggered_by):
            return
    review_pr.start(conn, github, settings, pr, enrolment, triggered_by)


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
        if workflow["status"] == base.ACTIVE:
            attempts.requeue(ctx.conn, run, attempt=run["attempt"])
        return
    if workflow["status"] == base.ACTIVE and attempts.is_bad(run) and attempts.retry(ctx.conn, run):
        return
    try:
        BY_KIND[workflow["kind"]].on_result(ctx, run, workflow)
    except bundles.Lost as e:
        _lost(ctx, run, workflow, e)


def _lost(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, why: bundles.Lost) -> None:
    """A run whose commits are gone for good is handled once: a notice saying
    how to run it again, and `needs-a-human`, on its ticket (a review's: on
    its PR). A spec's ticket stops alone; the spec's queue goes on without
    it. Any other workflow stops."""
    log.error("run %s: its commits are lost: %s", run["id"], why,
              extra=about_row(run))
    repo, subject = run["repo"], int(workflow["subject"])
    review = run["task"] == "review"
    number = json.loads(run["dispatch"])["pr_number"] if review else int(run["ticket"])
    ctx.github.post_comment(repo, number, render_notice(
        f"Factory run {run['id']} finished, but its commits were lost: the factory's git store "
        "no longer has them and the runner's files have expired. Nothing was pushed. Add "
        f"`{'agent:review' if review else 'agent:implement'}` again to run it again."))
    ctx.github.add_labels(repo, number, ["needs-a-human"])
    if workflow["kind"] == implement_spec.KIND and run["task"] == "implement":
        ctx.github.remove_label(repo, number, base.IN_PROGRESS)
    elif workflow["status"] == base.ACTIVE:
        base.end(ctx, workflow, base.STOPPED, subject)


def prepare(ctx: Context, run: sqlite3.Row) -> dict[str, Any] | None:
    """The dispatch to send for a queued run, freshened by its workflow just
    before it goes out (e.g. implement's base), or None to send it as queued."""
    if run["workflow_id"] is None:
        return None
    workflow = base.get(ctx.conn, run["workflow_id"])
    if workflow is None:
        return None
    return BY_KIND[workflow["kind"]].prepare(ctx, run, workflow)
