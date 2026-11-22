"""The implement-ticket workflow: explore -> implement -> review, for one ticket
and one PR (orchestrator.md#implement-ticket).

A human starts it with `agent:explore`, joins it at implement with
`agent:implement` (refused while the ticket has an open factory PR), or reviews
its PR again with `agent:review`. Explore's verdict decides what happens
next (orchestrator.md#explore): `easy-call` queues implement; the others stop
the workflow. Implement starts from the default branch's head at dispatch
time; `built` pushes its checked bundle to `factory/<ticket>-<run id>` and
opens a draft PR, `needs-a-human` pushes nothing and stops the workflow
(orchestrator.md#implement). `built` also queues review of the PR, whose
fixes are pushed without force before one verdict comment is posted and the
PR marked ready (orchestrator.md#review). A PR closed without merging cancels
the workflow (`cancel_closed_prs`).
"""

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from factory import attempts, bundles, loop, review, tickets
from factory import enrolment as enrolments
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

KIND = "implement-ticket"


def start_explore(
    conn: sqlite3.Connection,
    github: GitHub,
    settings: "Settings",
    issue: dict[str, Any],
    enrolment: Enrolment,
    triggered_by: "Added | None",
    *,
    kind: str = KIND,
) -> int | None:
    """Start (or restart) the ticket's workflow with an explore run: the row is
    queued and the ticket marked in progress. Returns the run id, or None if the ticket is too
    big to dispatch, when a notice says so and nothing starts. Implement-spec
    starts the same way, with spec explore (`kind`)."""
    repo, ticket = enrolment.repo, str(issue["number"])
    login = triggered_by.login if triggered_by else None
    try:
        dispatch = tickets.fit(
            {
                "contract_version": CONTRACT_VERSION,
                "repo": repo,
                "task": "explore",
                "ticket": ticket,
                "older_issues": tickets.older_issues(github, repo, issue["number"]),
                "base_sha": github.default_branch_head(repo),
                "enrolment_sha": enrolment.sha,
                "model": settings.model("explore", enrolment),
                "timeout_minutes": settings.timeout("explore", enrolment),
            },
            tickets.fetch(github, repo, issue),
        )
    except tickets.TooBig as e:
        github.post_comment(repo, issue["number"], tickets.render_too_big_notice(str(e), login))
        log.info("%s#%s is too big to dispatch: %s", repo, ticket, e)
        return None
    workflow_id = base.start(conn, repo=repo, kind=kind, subject=ticket)
    run_id = loop.enqueue(conn, repo=repo, task="explore", ticket=ticket, dispatch=dispatch,
                          workflow_id=workflow_id, triggered_by=login,
                          triggered_by_id=triggered_by.id if triggered_by else None)
    log.info("queued explore run %s for %s#%s", run_id, repo, ticket,
             extra=about_run(id=run_id, repo=repo, ticket=ticket))
    base.mark_in_progress(github, repo, issue["number"])
    return run_id


def start_implement(
    conn: sqlite3.Connection,
    github: GitHub,
    settings: "Settings",
    issue: dict[str, Any],
    enrolment: Enrolment,
    triggered_by: "Added | None",
) -> int | None:
    """A human added `agent:implement` to a ticket: the ticket's workflow
    joins at implement, skipping explore. Returns the run id, or None when a
    notice says why nothing was queued."""
    repo, number = enrolment.repo, issue["number"]
    ticket = str(number)
    if triggered_by is None or triggered_by.id is None:
        # Implement's commits credit whoever triggered the work; here no one is known.
        github.post_comment(repo, number, tickets.render_notice(
            "The issue's events don't say who added `agent:implement`, so nothing was started. "
            "Add it again."))
        return None
    pr = open_factory_pr(conn, github, repo, number)
    if pr is not None:
        # Force-pushing would lose commits added since, and a second PR would
        # leave two to choose between.
        github.post_comment(repo, number, tickets.render_notice(
            f"This ticket already has an open factory PR, #{pr}, so `agent:implement` was "
            "removed and nothing was started. To rebuild it, close the PR and add "
            "`agent:implement` again.", [triggered_by.login]))
        log.info("%s#%s already has factory PR #%s: implement not queued", repo, ticket, pr,
                 extra={"repo": repo, "ticket": ticket})
        return None
    workflow_id = base.start(conn, repo=repo, kind=KIND, subject=ticket)
    try:
        run_id = enqueue_implement(conn, github, settings, issue, repo, workflow_id,
                                    (triggered_by.login, triggered_by.id))
    except tickets.TooBig as e:
        github.post_comment(repo, number,
                            tickets.render_too_big_notice(str(e), triggered_by.login))
        workflow = base.get(conn, workflow_id)
        assert workflow is not None
        base.end(Context(conn, github, settings), workflow, base.STOPPED, number)
        log.info("%s#%s is too big to dispatch: %s", repo, ticket, e)
        return None
    log.info("queued implement run %s for %s#%s", run_id, repo, ticket,
             extra=about_run(id=run_id, repo=repo, ticket=ticket))
    base.mark_in_progress(github, repo, number)
    return run_id


def open_factory_pr(conn: sqlite3.Connection, github: GitHub, repo: str, number: int) -> int | None:
    """The ticket's open factory PR, if it has one: the PR in its workflow's
    row, or, if the row has none (e.g. the database was lost), an open PR the
    App opened whose closing references include the ticket."""
    row = conn.execute(
        "SELECT pr FROM workflows WHERE repo = ? AND kind = ? AND subject = ? AND pr IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (repo, KIND, str(number)),
    ).fetchone()
    if row is not None and github.pull(repo, row["pr"])["state"] == "open":
        return int(row["pr"])
    app = github.app_login()
    for pull in github.open_pulls(repo):
        if (pull.get("user") or {}).get("login") == app and number in github.closing_issues(
                repo, pull["number"]):
            return int(pull["number"])
    return None


def cancel_closed_prs(
    conn: sqlite3.Connection, github: GitHub, settings: "Settings", repo: str,
    issues: list[dict[str, Any]], *, full_scan: bool,
) -> None:
    """End the workflow of each factory PR a human closed without merging:
    `cancelled`, `agent:in-progress` off the ticket, and a notice on the
    ticket if it's still open. Its review's rows are cancelled by
    `orchestrator.cancel_closed`, which runs first.

    `issues` and `full_scan` are as for `base.closed_unmerged`."""
    for workflow in base.closed_unmerged(conn, github, repo, issues, kind=KIND,
                                         statuses=(base.ACTIVE, base.DONE), full_scan=full_scan):
        number = int(workflow["subject"])
        if github.get_issue(repo, number)["state"] == "open":
            last = conn.execute(
                "SELECT triggered_by FROM runs WHERE workflow_id = ? AND triggered_by IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (workflow["id"],)).fetchone()
            github.post_comment(repo, number, tickets.render_notice(
                f"PR #{workflow['pr']} was closed without merging, so the factory stopped "
                "working on this ticket. Add `agent:implement` to rebuild it.",
                [last["triggered_by"]] if last else None))
        base.end(Context(conn, github, settings), workflow, base.CANCELLED, number)
        log.info("%s#%s, the PR of #%s, was closed without merging: workflow cancelled", repo,
                 workflow["pr"], number, extra={"repo": repo, "ticket": workflow["subject"]})


def start_review(
    conn: sqlite3.Connection,
    github: GitHub,
    settings: "Settings",
    pr: dict[str, Any],
    enrolment: Enrolment,
    triggered_by: "Added | None",
) -> bool:
    """A human added `agent:review` to a PR: if the factory opened it for a
    ticket, review it again in that ticket's workflow (reactivated), against
    the ticket. False, doing nothing, for any other PR."""
    repo, number = enrolment.repo, pr["number"]
    workflow = base.of_pr(conn, repo, KIND, number)
    if workflow is None:
        return False
    head = github.pull(repo, number)["head"]["ref"]
    opened = next((r for r in conn.execute(
        "SELECT * FROM runs WHERE workflow_id = ? AND task = 'implement' AND outcome = 'succeeded' "
        "ORDER BY id DESC", (workflow["id"],))
        if json.loads(r["dispatch"]).get("branch") == head), None)
    if opened is None:
        return False
    if triggered_by is None or triggered_by.id is None:
        unknown_reviewer(github, repo, number)
        return True
    review_again(Context(conn, github, settings), workflow, opened, number, branch=head,
                 pr_base_sha=json.loads(opened["dispatch"])["base_sha"],
                 triggered_by=(triggered_by.login, triggered_by.id))
    return True


def unknown_reviewer(github: GitHub, repo: str, number: int) -> None:
    """The notice when no one is known to have added `agent:review`: review's
    commits credit whoever asked for it."""
    github.post_comment(repo, number, tickets.render_notice(
        "The PR's events don't say who added `agent:review`, so nothing was started. "
        "Add it again."))


def review_again(
    ctx: Context, workflow: sqlite3.Row, after: sqlite3.Row, number: int, *, branch: str,
    pr_base_sha: str, triggered_by: tuple[str, int],
) -> None:
    """A human re-added `agent:review` to PR `number`: the workflow is
    reactivated and the review queued (`queue_review`), the PR's stale status
    labels come off and its subject is `agent:in-progress`."""
    base.start(ctx.conn, repo=workflow["repo"], kind=workflow["kind"],
               subject=workflow["subject"])
    workflow = base.get(ctx.conn, workflow["id"])
    assert workflow is not None
    if queue_review(ctx, after, workflow, number, branch=branch, pr_base_sha=pr_base_sha,
                    triggered_by=triggered_by) is None:
        return
    for label in base.STALE:
        ctx.github.remove_label(workflow["repo"], number, label)
    base.mark_in_progress(ctx.github, workflow["repo"], int(workflow["subject"]))


def on_result(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Act on a done run. A cancelled run (its ticket closed, or `factory
    cancel`) cancels the workflow. A bad run reaches here only once it was the
    last attempt (`factory.attempts` retries the others): the workflow gives
    up. Otherwise explore's, implement's and review's results are handled,
    including a review whose fixes failed the check (`review.output_used`)."""
    if run["outcome"] == "cancelled":
        if workflow["status"] == base.ACTIVE:
            base.end(ctx, workflow, base.CANCELLED, int(run["ticket"]))
    elif attempts.is_bad(run):
        if workflow["status"] == base.ACTIVE:
            give_up(ctx, run, workflow)
    elif run["task"] == "explore" and run["outcome"] == "succeeded":
        _explored(ctx, run, workflow, json.loads(run["result"])["output"])
    elif run["task"] == "implement" and run["outcome"] == "succeeded":
        _implemented(ctx, run, workflow, json.loads(run["result"])["output"])
    elif review.output_used(run):
        reviewed(ctx, run, workflow, json.loads(run["result"]))


def reviewed(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, result: dict[str, Any]) -> None:
    """Review's result (orchestrator.md#review): its fixes pushed without
    force (or discarded, if they failed the check), one verdict comment, then
    the PR marked ready and its human asked to review it. If the branch moved
    while review ran, the fixes are discarded and review is queued again."""
    repo, ticket = run["repo"], int(run["ticket"])
    dispatch = json.loads(run["dispatch"])
    pr = dispatch["pr_number"]
    discarded = result["outcome"] == "failed"
    if not discarded:
        store = base.store(ctx)
        checked = checked_bundle(ctx, store, run, dispatch["base_sha"])
        if checked is not None:
            try:
                store.push(repo, run["id"], dispatch["branch"])
            except bundles.Moved:
                _branch_moved(ctx, run, workflow)
                return
    link = ctx.runner.log_link(repo, run["runner_run_id"])
    review.publish(ctx.github, repo, pr, run["id"], result, link, discarded=discarded)
    review.finish(ctx.github, repo, pr,
                  needs_a_human=discarded or review.changes_requested(result["output"]),
                  reviewer=run["triggered_by"])
    base.end(ctx, workflow, base.DONE, ticket)
    log.info("review run %s posted on %s#%s%s", run["id"], repo, pr,
             " (fixes discarded: they failed the check)" if discarded else "",
             extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))


def _branch_moved(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """The PR's branch got new commits while review ran: review looked at a
    head that no longer exists. Its fixes are discarded (`superseded`) and
    review is queued again as the next attempt, from the new head
    (`factory.attempts`); out of attempts, the workflow gives up."""
    with ctx.conn:
        ctx.conn.execute("UPDATE runs SET outcome = 'superseded' WHERE id = ?", (run["id"],))
    run = loop.get_run(ctx.conn, run["id"])
    assert run is not None
    log.info("review run %s: %s moved while it ran; its fixes were discarded", run["id"],
             json.loads(run["dispatch"])["branch"],
             extra=about_run(id=run["id"], repo=run["repo"], ticket=run["ticket"]))
    if attempts.retry(ctx.conn, run) is None:
        give_up(ctx, run, workflow)


def prepare(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> dict[str, Any] | None:
    """Implement starts from the default branch's head at dispatch time, for
    every attempt, so a row held back by the usage pause or a retry doesn't
    build on stale code (orchestrator.md#implement-ticket)."""
    dispatch = json.loads(run["dispatch"])
    if run["task"] == "implement":
        return {**dispatch, "base_sha": ctx.github.default_branch_head(run["repo"])}
    if run["task"] == "review":
        # Review looks at the PR as it is when the run starts, so a human's
        # push while it waited doesn't make its fixes land on a moved branch.
        head = base.store(ctx).branch_head(run["repo"], dispatch["branch"])
        return {**dispatch, "base_sha": head} if head else None
    return None


def _test_paths(ctx: Context, repo: str) -> list[str]:
    enrolment = enrolments.get(ctx.conn, repo)
    return list(enrolment.config.test_paths or []) if enrolment else []


def queue_review(
    ctx: Context, after: sqlite3.Row, workflow: sqlite3.Row, pr: int, *, branch: str,
    pr_base_sha: str, triggered_by: tuple[str, int] | None = None,
) -> int | None:
    """Queue review of the workflow's PR, from the branch's head now (`prepare`
    freshens it at dispatch). It copies `after`'s `triggered_by` unless a human
    re-added `agent:review` (`triggered_by`). None if the PR and its ticket are
    too big to dispatch: a notice on the PR says so and the workflow stops."""
    repo, ticket = workflow["repo"], workflow["subject"]
    login, user = triggered_by or (after["triggered_by"], after["triggered_by_id"])
    enrolment = enrolments.get(ctx.conn, repo)
    assert enrolment is not None  # an unenrolled repo's runs are never handled
    head = base.store(ctx).branch_head(repo, branch)
    if head is None:
        raise bundles.GitError(f"{branch}, PR #{pr}'s branch, isn't on {repo}")
    pr_issue = ctx.github.get_issue(repo, pr)
    specs = [ctx.github.get_issue(repo, n) for n in review.against(ctx.conn, ctx.github, repo, pr)]
    try:
        dispatch = tickets.fit(
            {
                "contract_version": CONTRACT_VERSION,
                "repo": repo,
                "task": "review",
                "ticket": ticket,
                "pr_number": pr,
                "pr_base_sha": pr_base_sha,
                "findings_only": False,
                "base_sha": head,
                "branch": branch,
                "triggered_by": {"login": login, "id": user},
                "enrolment_sha": enrolment.sha,
                "model": ctx.settings.model("review", enrolment),
                "timeout_minutes": ctx.settings.timeout("review", enrolment),
            },
            review.render_input(ctx.github, repo, pr_issue, specs),
        )
    except tickets.TooBig as e:
        ctx.github.post_comment(repo, pr, tickets.render_too_big_notice(str(e), login))
        base.end(ctx, workflow, base.STOPPED, int(ticket), "needs-a-human")
        log.info("%s#%s is too big to review: %s", repo, pr, e, extra={"repo": repo})
        return None
    run_id = loop.enqueue(ctx.conn, repo=repo, task="review", ticket=ticket, dispatch=dispatch,
                          workflow_id=workflow["id"], triggered_by=login, triggered_by_id=user)
    log.info("queued review run %s of %s#%s for #%s", run_id, repo, pr, ticket,
             extra=about_run(id=run_id, repo=repo, ticket=ticket))
    return run_id


def _implemented(
    ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, output: dict[str, Any]
) -> None:
    """Implement's verdict (orchestrator.md#implement). The result, including
    its bundle, was checked when it was collected."""
    repo, number = run["repo"], int(run["ticket"])
    link = ctx.runner.log_link(repo, run["runner_run_id"])
    if output["verdict"] == "needs-a-human":
        ctx.github.post_comment(repo, number,
                                render_needs_a_human_comment(run["id"], output["comment"], link))
        base.end(ctx, workflow, base.STOPPED, number, "needs-a-human")
        log.info("implement run %s needs a human on %s#%s", run["id"], repo, number,
                 extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))
        return
    dispatch = json.loads(run["dispatch"])
    store = base.store(ctx)
    checked = checked_bundle(ctx, store, run, dispatch["base_sha"])
    assert checked is not None  # built always has commits
    store.push(repo, run["id"], dispatch["branch"])
    issue = ctx.github.get_issue(repo, number)
    enrolment = enrolments.get(ctx.conn, repo)
    test_paths = enrolment.config.test_paths if enrolment else None
    notes = []
    if test_paths and not any(bundles.matches(f, test_paths) for f in checked.files):
        notes.append("⚠️ no test files changed")
    if ticket_changed(ctx, run, dispatch, issue):
        notes.append("⚠️ the ticket changed after this run started")
    body = render_pr_body(number, run["id"], link, output.get("pr_body"), checked.log, notes)
    pr = ctx.github.open_draft_pull(repo, head=dispatch["branch"],
                                    base=ctx.github.default_branch(repo),
                                    title=issue["title"], body=body)
    with ctx.conn:
        ctx.conn.execute("UPDATE workflows SET pr = ? WHERE id = ?", (pr, workflow["id"]))
    log.info("implement run %s built %s#%s: draft PR #%s from %s", run["id"], repo, number, pr,
             dispatch["branch"], extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))
    if not ctx.conn.execute(
        "SELECT 1 FROM runs WHERE workflow_id = ? AND task = 'review' AND id > ?",
        (run["workflow_id"], run["id"]),
    ).fetchone():
        queue_review(ctx, run, workflow, pr, branch=dispatch["branch"],
                      pr_base_sha=dispatch["base_sha"])


def checked_bundle(
    ctx: Context, store: bundles.Store, run: sqlite3.Row, base_sha: str
) -> bundles.Checked | None:
    """The run's bundle as checked on collection, or None for a review that
    made no fixes. If the store lost it (e.g. it was wiped), the run's files
    are fetched and checked again; `bundles.Lost` if the runner has none."""
    checked = store.checked(run["repo"], run["id"], base_sha)
    if checked is not None:
        return checked
    files = ctx.runner.files(run["repo"], run["runner_run_id"])
    if files is None:
        raise bundles.Lost(f"run {run['id']}'s files are gone from the runner")
    if files.bundle is None and run["task"] == "review":
        return None  # review's bundle may be empty: no bundle at all
    if files.bundle is None:
        raise bundles.Lost(f"run {run['id']}'s bundle is gone from the runner")
    dispatch = json.loads(run["dispatch"])
    output = json.loads(run["result"]).get("output") or {}
    return store.check(run["repo"], run["id"], files.bundle, task=run["task"],
                       verdict=output.get("verdict"), base_sha=base_sha,
                       pr_base_sha=dispatch.get("pr_base_sha"),
                       test_paths=_test_paths(ctx, run["repo"]))


def ticket_changed(
    ctx: Context, run: sqlite3.Row, dispatch: dict[str, Any], issue: dict[str, Any],
    ticket: tickets.Ticket | None = None,
) -> bool:
    """Whether the ticket was edited, or got a comment from someone with write
    access, after the run's `ticket.md` was rendered. `ticket` is the ticket
    as it renders now (default: the issue and its comments); its orchestrator
    sections count as part of the ticket."""
    ticket = ticket or tickets.fetch(ctx.github, run["repo"], issue)
    rendered = dispatch["ticket_markdown"]
    if rendered.startswith("> Trimmed"):
        rendered = rendered.split("\n", 2)[2]
    head = tickets.render(replace(ticket, comments=[]))
    if not rendered.startswith(head):
        return True
    rest = rendered[len(head):]
    if rest and not rest.startswith("\n## "):
        return True
    since = _when(run["created_at"])  # when its ticket.md was rendered
    app = ctx.github.app_login()
    return any(_when(c.created_at) > since for c in ticket.comments if c.author != app)


def _when(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def give_up(
    ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, again: str | None = None,
    stop: bool = True,
) -> None:
    """The ticket ran out of attempts: a notice, `agent:blocked`, and the
    workflow stops. For review they go on the PR, which stays a draft: marking
    it ready would say someone had read it. `again` is the label that starts
    over (default: the task's own). With `stop` False (a spec ticket, whose
    spec goes on without it), only the ticket is labelled."""
    repo, number = run["repo"], int(run["ticket"])
    if run["task"] == "review":
        number = json.loads(run["dispatch"])["pr_number"]
    ended = attempts.failure(run) or run["outcome"]
    link = ctx.runner.log_link(repo, run["runner_run_id"]) if run["runner_run_id"] else None
    last = f"[run {run['id']}]({link})" if link else f"run {run['id']}"
    message = (
        f"The factory gave up on `{run['task']}` after {run['attempt']} attempts: the last, "
        f"{last}, ended `{ended}`. Add `{again or 'agent:' + run['task']}` to try again from "
        "attempt 1."
    )
    ctx.github.post_comment(repo, number, tickets.render_notice(
        message, [run["triggered_by"]] if run["triggered_by"] else None))
    if run["task"] == "review":
        ctx.github.add_labels(repo, number, ["agent:blocked"])
        base.end(ctx, workflow, base.STOPPED, int(run["ticket"]))
    elif not stop:
        ctx.github.add_labels(repo, number, ["agent:blocked"])
        ctx.github.remove_label(repo, number, base.IN_PROGRESS)
    else:
        base.end(ctx, workflow, base.STOPPED, number, "agent:blocked")
    log.info("gave up on %s#%s after run %s ended %s", repo, number, run["id"], ended,
             extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))


def _explored(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, output: dict[str, Any]) -> None:
    """Explore's verdict (orchestrator.md#explore)."""
    repo, number = run["repo"], int(run["ticket"])
    verdict = output["verdict"]
    if explore_stopped(ctx, run, workflow, output):
        return
    if verdict == "easy-call":
        issue = ctx.github.get_issue(repo, number)
        why_not = _why_not_implement(ctx, run, issue)
        if why_not is not None:
            post_explore(ctx, run, output, [why_not])
            base.end(ctx, workflow, base.STOPPED, number, "needs-a-human")
            return
        post_explore(ctx, run, output)
        _queue_implement(ctx, run, issue)
    elif verdict == "too-big":
        post_explore(ctx, run, output, ["Add `agent:implement-spec` to build this as a spec."])
        base.end(ctx, workflow, base.STOPPED, number, "needs-a-human")


def post_explore(
    ctx: Context, run: sqlite3.Row, output: dict[str, Any], notes: list[str] | None = None,
    heading: str = "Explore findings",
) -> None:
    """Post explore run `run`'s findings on its ticket."""
    repo, number = run["repo"], int(run["ticket"])
    link = ctx.runner.log_link(repo, run["runner_run_id"])
    ctx.github.post_comment(repo, number,
                            render_explore_comment(run["id"], output, link, notes, heading))
    log.info("posted explore findings of run %s on %s#%s (%s)", run["id"], repo, number,
             output["verdict"], extra=about_run(id=run["id"], repo=repo, ticket=run["ticket"]))


def explore_stopped(
    ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, output: dict[str, Any],
    heading: str = "Explore findings",
) -> bool:
    """The verdicts that stop a workflow at explore, whatever the workflow:
    `needs-a-human` and `waiting` label the issue, and `duplicate` closes it
    (or, if the duplicate has been closed since, asks a human). False, doing
    nothing, for any other verdict."""
    repo, number = run["repo"], int(run["ticket"])
    verdict = output["verdict"]
    if verdict in ("needs-a-human", "waiting"):
        post_explore(ctx, run, output, heading=heading)
        base.end(ctx, workflow, base.STOPPED, number, verdict)
    elif verdict == "duplicate":
        duplicate_of = output["duplicate_of"]
        if ctx.github.get_issue(repo, int(duplicate_of))["state"] != "open":
            post_explore(ctx, run, output, [
                f"#{duplicate_of} has been closed since, so this ticket was left open: only a "
                "human can tell whether its fix covers this one too."], heading)
            base.end(ctx, workflow, base.STOPPED, number, "needs-a-human")
            return True
        # The findings first: a reopened duplicate's history is read from the
        # App's explore comment ("### Duplicate of") before the App's close.
        post_explore(ctx, run, output, heading=heading)
        ctx.github.close_as_duplicate(repo, number)
        base.end(ctx, workflow, base.STOPPED, number)
    else:
        return False
    return True


def _why_not_implement(ctx: Context, run: sqlite3.Row, issue: dict[str, Any]) -> str | None:
    """Why an `easy-call` can't go on to implement by itself, or None if it can.

    An outsider's ticket waits for someone with write access to read the plan
    first, so code is never written from an outsider's text unread.
    """
    enrolment = enrolments.get(ctx.conn, run["repo"])
    if enrolment is None or "implement" not in enrolment.config.enabled_tasks():
        return ("`implement` isn't enabled for this repo (see `tasks` in `.factory/config.yml`), "
                "so nothing is built.")
    author = issue["user"]["login"]
    if ctx.github.permission(run["repo"], author) not in tickets.WRITERS:
        return (f"@{author}, who opened this ticket, doesn't have write access, so nothing is "
                "built until someone who does reads the plan and adds `agent:implement`.")
    if run["triggered_by"] is None or run["triggered_by_id"] is None:
        # Implement's commits credit whoever triggered the work; here no one is known.
        return ("The issue's events don't say who added `agent:explore`. Nothing is built "
                "until someone with write access adds `agent:implement`.")
    return None


def _queue_implement(ctx: Context, after: sqlite3.Row, issue: dict[str, Any]) -> None:
    """Queue implement for the ticket, chained from run `after` (whose
    `triggered_by` it copies). Nothing if `after` already queued one: a crash
    between queueing and marking `after` handled must not queue it twice."""
    if ctx.conn.execute(
        "SELECT 1 FROM runs WHERE workflow_id = ? AND task = 'implement' AND id > ?",
        (after["workflow_id"], after["id"]),
    ).fetchone():
        return
    repo, ticket = after["repo"], after["ticket"]
    run_id = enqueue_implement(ctx.conn, ctx.github, ctx.settings, issue, repo,
                                after["workflow_id"],
                                (after["triggered_by"], after["triggered_by_id"]))
    log.info("queued implement run %s for %s#%s after explore run %s", run_id, repo, ticket,
             after["id"], extra=about_run(id=run_id, repo=repo, ticket=ticket))


def enqueue_implement(
    conn: sqlite3.Connection, github: GitHub, settings: "Settings", issue: dict[str, Any],
    repo: str, workflow_id: int, triggered_by: tuple[str, int], *,
    ticket_md: tickets.Ticket | None = None, base_sha: str | None = None,
) -> int:
    """Queue implement for `issue` in workflow `workflow_id`, crediting
    `triggered_by` (login, user id). `ticket_md` is the ticket as rendered
    into `ticket.md` (default: the issue and its comments), `base_sha` where
    it starts (default: the default branch's head; `prepare` freshens it at
    dispatch). Raises `tickets.TooBig` if the ticket is too big to dispatch."""
    ticket = str(issue["number"])
    login, user = triggered_by
    enrolment = enrolments.get(conn, repo)
    assert enrolment is not None  # an unenrolled repo's runs are never handled
    dispatch = tickets.fit(
        {
            "contract_version": CONTRACT_VERSION,
            "repo": repo,
            "task": "implement",
            "ticket": ticket,
            "base_sha": base_sha or github.default_branch_head(repo),
            # Sized with the longest run id; `enqueue` names it with the real one.
            "branch": _branch(ticket, tickets.RUN_ID_ROOM),
            "triggered_by": {"login": login, "id": user},
            "enrolment_sha": enrolment.sha,
            "model": settings.model("implement", enrolment),
            "timeout_minutes": settings.timeout("implement", enrolment),
        },
        ticket_md or tickets.fetch(github, repo, issue),
    )
    return loop.enqueue(conn, repo=repo, task="implement", ticket=ticket, dispatch=dispatch,
                        workflow_id=workflow_id, triggered_by=login, triggered_by_id=user,
                        branch=lambda run_id: _branch(ticket, run_id))


def _branch(ticket: str, run_id: int) -> str:
    return f"factory/{ticket}-{run_id}"


DRAFT_NOTICE = ("This PR stays a draft until the factory's review is posted. "
                "A human merges it.")


def render_pr_body(
    number: int, run_id: int, log_link: str, pr_body: str | None, log_lines: list[str],
    notes: list[str],
) -> str:
    """The draft PR's body: implement's `pr_body`, or the commit log without
    one, then the orchestrator's footer."""
    if pr_body and pr_body.strip():
        summary = pr_body.strip()
    else:
        summary = "Commits:\n\n```\n" + "\n".join(log_lines) + "\n```"
    footer = [f"Closes #{number}", *notes, f"Factory run {run_id} · [runner log]({log_link})",
              DRAFT_NOTICE]
    return f"{summary}\n\n---\n" + "\n\n".join(footer) + "\n"


def render_needs_a_human_comment(run_id: int, comment: str, log_link: str) -> str:
    """Implement's `needs-a-human` comment, under the factory's own heading."""
    return (f"<!-- factory:implement run={run_id} -->\n## Implement needs a human\n\n"
            f"{comment.strip()}\n\nNothing was pushed. Once the ticket is settled, add "
            f"`agent:implement` to build it.\n\n---\nFactory run {run_id} · "
            f"[runner log]({log_link})\n")


def render_explore_comment(
    run_id: int, output: dict[str, Any], log_link: str, notes: list[str] | None = None,
    heading: str = "Explore findings",
) -> str:
    """Explore's findings under the factory's own headings, the same every time.
    `notes` are the orchestrator's own lines, put above the footer."""
    parts = [
        f"<!-- factory:explore run={run_id} -->\n## {heading}\n",
        f"**Verdict:** `{output['verdict']}`\n",
        render_explore_sections(output),
    ]
    parts.extend(f"{note}\n" for note in notes or [])
    parts.append(f"---\nFactory run {run_id} · [runner log]({log_link})\n")
    return "\n".join(parts)


def render_explore_sections(output: dict[str, Any]) -> str:
    """Explore's findings as `###` sections: summary, findings, plan, files,
    and questions or the duplicate if it has them."""

    def section(heading: str, text: str) -> str:
        return f"### {heading}\n\n{text.strip()}\n"

    def listed(items: list[str], numbered: bool) -> str:
        if not items:
            return "None."
        return "\n".join(f"{i}. {x}" if numbered else f"- {x}" for i, x in enumerate(items, 1))

    parts = [
        section("Summary", output["summary"]),
        section("Findings", output["findings"]),
        section("Plan", listed(output["plan"], numbered=True)),
        section("Files", listed([f"`{f}`" for f in output["files"]], numbered=False)),
    ]
    if output.get("questions"):
        parts.append(section("Questions", listed(output["questions"], numbered=True)))
    if output.get("duplicate_of"):
        parts.append(section("Duplicate of", f"#{output['duplicate_of']}"))
    return "\n".join(parts)
