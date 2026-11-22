"""The review-pr workflow: a findings-only review of a PR the factory didn't
open (orchestrator.md#review).

A human starts it with `agent:review` on a same-repo PR that isn't a factory
PR (those are reviewed in their implement-ticket workflow, and fork PRs never).
The review pushes nothing: changes it would have made become findings. Its
findings and verdict are posted, but the PR isn't marked ready, labelled for
a human or anyone asked to review it: whoever asked wanted an opinion, not
commits on their branch. The workflow's subject is the PR, and so is each
run's `ticket`, so closing the PR cancels it.
"""

import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from factory import attempts, loop, review, tickets
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

KIND = "review-pr"


def start(
    conn: sqlite3.Connection,
    github: GitHub,
    settings: "Settings",
    pr: dict[str, Any],
    enrolment: Enrolment,
    triggered_by: "Added | None",
) -> int | None:
    """Queue a findings-only review of PR `pr` (as an issue) and mark the PR
    in progress. Returns the run id, or None when nothing was queued (a
    notice on the PR says why)."""
    repo, number = enrolment.repo, pr["number"]
    if triggered_by is None or triggered_by.id is None:
        # Review's dispatch names whoever asked for it; here no one is known.
        github.post_comment(repo, number, tickets.render_notice(
            "The PR's events don't say who added `agent:review`, so nothing was started. "
            "Add it again."))
        return None
    pull = github.pull(repo, number)
    specs = [github.get_issue(repo, n) for n in review.against(conn, github, repo, number)]
    try:
        dispatch = tickets.fit(
            {
                "contract_version": CONTRACT_VERSION,
                "repo": repo,
                "task": "review",
                "ticket": str(number),
                "pr_number": number,
                "pr_base_sha": pull["base"]["sha"],
                "findings_only": True,
                "base_sha": pull["head"]["sha"],
                "branch": pull["head"]["ref"],
                "triggered_by": {"login": triggered_by.login, "id": triggered_by.id},
                "enrolment_sha": enrolment.sha,
                "model": settings.model("review", enrolment),
                "timeout_minutes": settings.timeout("review", enrolment),
            },
            review.render_input(github, repo, pr, specs),
        )
    except tickets.TooBig as e:
        github.post_comment(repo, number, tickets.render_too_big_notice(str(e), triggered_by.login))
        log.info("%s#%s is too big to review: %s", repo, number, e, extra={"repo": repo})
        return None
    workflow_id = base.start(conn, repo=repo, kind=KIND, subject=str(number))
    run_id = loop.enqueue(conn, repo=repo, task="review", ticket=str(number), dispatch=dispatch,
                          workflow_id=workflow_id, triggered_by=triggered_by.login,
                          triggered_by_id=triggered_by.id)
    log.info("queued findings-only review run %s of %s#%s", run_id, repo, number,
             extra=about_run(id=run_id, repo=repo, ticket=str(number)))
    base.mark_in_progress(github, repo, number)
    return run_id


def prepare(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> dict[str, Any] | None:
    """Review looks at the PR as it is when the run starts, not when it was queued."""
    dispatch = json.loads(run["dispatch"])
    head = ctx.github.pull(run["repo"], dispatch["pr_number"])["head"]["sha"]
    return {**dispatch, "base_sha": head}


def on_result(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Act on a done run: a cancelled one (the PR closed) cancels the
    workflow, the last bad attempt gives up, and a used result is posted."""
    number = int(workflow["subject"])
    if workflow["status"] != "active":
        return
    if run["outcome"] == "cancelled":
        base.end(ctx, workflow, "cancelled", number)
    elif attempts.is_bad(run):
        _give_up(ctx, run, workflow)
    elif review.output_used(run):
        link = ctx.runner.log_link(run["repo"], run["runner_run_id"])
        review.publish(ctx.github, run["repo"], number, run["id"], json.loads(run["result"]), link)
        base.end(ctx, workflow, "done", number)
        log.info("findings-only review run %s posted on %s#%s", run["id"], run["repo"], number,
                 extra=about_run(id=run["id"], repo=run["repo"], ticket=run["ticket"]))


def _give_up(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Out of attempts: a notice and `agent:blocked` on the PR."""
    repo, number = run["repo"], int(workflow["subject"])
    ended = attempts.failure(run) or run["outcome"]
    link = ctx.runner.log_link(repo, run["runner_run_id"]) if run["runner_run_id"] else None
    last = f"[run {run['id']}]({link})" if link else f"run {run['id']}"
    ctx.github.post_comment(repo, number, tickets.render_notice(
        f"The factory gave up on `review` after {run['attempt']} attempts: the last, {last}, "
        f"ended `{ended}`. Add `agent:review` to try again from attempt 1.",
        [run["triggered_by"]] if run["triggered_by"] else None))
    base.end(ctx, workflow, "stopped", number, "agent:blocked")
    log.info("gave up on reviewing %s#%s after run %s ended %s", repo, number, run["id"], ended,
             extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))
