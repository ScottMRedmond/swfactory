"""The implement-spec workflow: spec explore -> auto-tickets -> implement each
ticket -> one review, for a whole spec on one integration branch
(orchestrator.md#implement-spec).

A human starts it with `agent:implement-spec` (`start`): the issue is
labelled `spec` and spec explore runs on it, unless the spec still has open
`spec:<n>` tickets or a leftover integration branch. Spec explore's
`easy-call` posts the spec exploration notes and queues auto-tickets with
them; its other verdicts stop the workflow as for a ticket. Auto-tickets'
`decomposed` creates the tickets (orchestrator.md#auto-tickets), then the
integration branch `factory/spec-<n>` at auto-tickets' `base_sha`, and stores
the spec body's hash. `spec_ticket` renders a spec ticket with the notes.

The build queue: each pass, `advance` queues implement for the
lowest-numbered buildable ticket of every active spec (`queue`), on the
integration branch's head, at most `per_spec_concurrency` at a time; `built`
lands the commits with a plain merge (`_land`), and `agent:implement` on a
spec ticket rejoins it to the queue (`start_ticket`). The spec PR (#33) and
stalls (#34) are added by their tickets.
"""

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from factory import enrolment as enrolments
from factory import attempts, bundles, loop, tickets
from factory.contract import CONTRACT_VERSION
from factory.enrolment import Enrolment
from factory.github import GitHub, GitHubError
from factory.logs import about_run
from factory.runner import Runner
from factory.tickets import render_notice
from factory.workflows import base, implement_ticket
from factory.workflows.base import Context
from factory.workflows.implement_ticket import render_explore_sections

if TYPE_CHECKING:
    from factory.orchestrator import Settings
    from factory.triggers import Added

log = logging.getLogger(__name__)

KIND = "implement-spec"
TRIGGER = "agent:implement-spec"
SPEC = "spec"  # the label on a spec issue
NOTES_HEADING = "Spec exploration notes"
EXPLORE_HEADING = "Spec explore findings"  # when spec explore stops the workflow

SPEC_LABEL_COLOR = "c5def5"
_MARKER = re.compile(r"<!-- factory:auto-tickets run=(\d+) index=(\d+) -->")


def spec_label(spec: int | str) -> str:
    return f"spec:{spec}"


def is_spec_ticket(issue: dict[str, Any]) -> bool:
    """Whether the issue is a ticket of a spec: it carries a `spec:<n>` label."""
    return any(n.startswith("spec:") for n in _labels(issue))


def start(
    conn: sqlite3.Connection,
    github: GitHub,
    settings: "Settings",
    issue: dict[str, Any],
    enrolment: Enrolment,
    triggered_by: "Added | None",
) -> int | None:
    """A human added `agent:implement-spec` to a spec: label it `spec` and
    start the workflow with spec explore. Returns the explore run's id, or
    None when a notice says why nothing was queued."""
    repo, number = enrolment.repo, issue["number"]
    if triggered_by is None or triggered_by.id is None:
        # The tickets' commits credit whoever triggered the work; here no one is known.
        github.post_comment(repo, number, render_notice(
            f"The issue's events don't say who added `{TRIGGER}`, so nothing was started. "
            "Add it again."))
        return None
    login = triggered_by.login
    github.add_labels(repo, number, [SPEC])
    if _refuse_open_tickets(github, repo, number, login):
        return None
    branch = integration_branch(number)
    if bundles.Store(settings.git_dir, github.git_remote).branch_head(repo, branch):
        github.post_comment(repo, number, render_notice(
            f"The integration branch `{branch}` already exists, from an earlier start, so "
            "nothing was started (the factory never force-pushes). Delete or rename it, then "
            "add `agent:implement-spec` again.", [login] if login else None))
        log.info("%s#%s: %s already exists; implement-spec not started", repo, number, branch,
                 extra={"repo": repo, "ticket": str(number)})
        return None
    return implement_ticket.start_explore(conn, github, settings, issue, enrolment,
                                          triggered_by, kind=KIND)


def _refuse_open_tickets(github: GitHub, repo: str, spec: int, mention: str | None) -> bool:
    """If the spec already has open `spec:<n>` tickets, a notice says to close
    them first, and True: auto-tickets would make a second set."""
    label = spec_label(spec)
    numbers = sorted(i["number"] for i in github.open_issues_with_label(repo, label)
                     if "pull_request" not in i)
    if not numbers:
        return False
    listed = ", ".join(f"#{n}" for n in numbers)
    github.post_comment(repo, spec, render_notice(
        f"This spec already has open tickets labelled `{label}` ({listed}), so nothing was "
        "started: auto-tickets would make a second set. To start over, close them and add "
        "`agent:implement-spec` again.", [mention] if mention else None))
    log.info("%s#%s already has open tickets %s: implement-spec not started", repo, spec,
             listed, extra={"repo": repo, "ticket": str(spec)})
    return True


def on_result(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Act on a done run of the workflow. A cancelled run cancels it, unless
    it's a spec ticket's implement: see `_ticket_result`."""
    if run["task"] == "implement":
        _ticket_result(ctx, run, workflow)
    elif run["outcome"] == "cancelled":
        if workflow["status"] == "active":
            base.end(ctx, workflow, "cancelled", int(run["ticket"]))
    elif attempts.is_bad(run):
        # The last attempt: `factory.attempts` retried the others.
        if workflow["status"] == "active":
            implement_ticket.give_up(ctx, run, workflow, again=TRIGGER)
    elif run["task"] == "explore" and run["outcome"] == "succeeded":
        _explored(ctx, run, workflow, json.loads(run["result"])["output"])
    elif run["task"] == "auto-tickets" and run["outcome"] == "succeeded":
        output = json.loads(run["result"])["output"]
        if output["verdict"] == "decomposed":
            _decomposed(ctx, run, workflow, output["tickets"])
        else:
            _needs_a_human(ctx, run, workflow, output["comment"])


LANDED_MARK = "<!-- factory:landed run={} -->"


def _ticket_result(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """A spec ticket's implement ended. Whatever happens to the ticket, the
    workflow goes on: the rest of the queue keeps moving. `built` lands the
    commits (`_land`); `needs-a-human`, or the last attempt ending badly,
    labels the ticket, which then holds back only what it blocks. A
    cancelled run (the ticket closed, or `factory cancel`) just leaves the
    ticket. A workflow cancelled meanwhile lands nothing."""
    repo, number = run["repo"], int(run["ticket"])
    if run["outcome"] == "cancelled" or workflow["status"] == "cancelled":
        ctx.github.remove_label(repo, number, base.IN_PROGRESS)
    elif attempts.is_bad(run):
        implement_ticket.give_up(ctx, run, workflow, again="agent:implement", stop=False)
    elif run["outcome"] == "succeeded":
        output = json.loads(run["result"])["output"]
        if output["verdict"] == "built":
            _land(ctx, run, workflow, output)
            return
        link = ctx.runner.log_link(repo, run["runner_run_id"])
        ctx.github.post_comment(repo, number, implement_ticket.render_needs_a_human_comment(
            run["id"], output["comment"], link))
        ctx.github.add_labels(repo, number, ["needs-a-human"])
        ctx.github.remove_label(repo, number, base.IN_PROGRESS)
        log.info("implement run %s needs a human on %s#%s, a ticket of spec #%s", run["id"],
                 repo, number, workflow["subject"], extra=_about(run))


def _land(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, output: dict[str, Any]) -> None:
    """Land a built ticket on the integration branch with a plain `git merge`
    (a fast-forward unless a human pushed meanwhile), then label it
    `agent:built` with a comment holding its `pr_body`; it stays open until
    the spec PR closes it. A conflict discards the commits and queues the
    ticket again on the new head, as the next attempt (`factory.attempts`);
    out of attempts, the ticket is `agent:blocked`."""
    repo, number = run["repo"], int(run["ticket"])
    dispatch = json.loads(run["dispatch"])
    branch = workflow["branch"]
    store = _store(ctx)
    checked = implement_ticket.checked_bundle(ctx, store, run, dispatch["base_sha"])
    assert checked is not None  # built always has commits
    app = ctx.github.app_login()
    issue = ctx.github.get_issue(repo, number)
    try:
        head = store.land(repo, run["id"], branch,
                          message=f"Merge #{number} ({issue['title']}) into {branch}\n\n"
                                  f"Factory run {run['id']}",
                          identity=(app, f"{app}@users.noreply.github.com"))
    except bundles.Conflict:
        _conflicted(ctx, run, workflow)
        return
    mark = LANDED_MARK.format(run["id"])
    if not any(mark in (c.get("body") or "") for c in ctx.github.issue_comments(repo, number)):
        link = ctx.runner.log_link(repo, run["runner_run_id"])
        notes = []
        enrolment = enrolments.get(ctx.conn, repo)
        test_paths = enrolment.config.test_paths if enrolment else None
        if test_paths and not any(bundles.matches(f, test_paths) for f in checked.files):
            notes.append("⚠️ no test files changed")
        if implement_ticket.ticket_changed(ctx, run, dispatch, issue,
                                           spec_ticket(ctx.conn, ctx.github, repo, issue,
                                                       workflow)):
            notes.append("⚠️ the ticket changed after this run started")
        ctx.github.post_comment(repo, number, render_landed_comment(
            run["id"], branch, head, output.get("pr_body"), checked.log, notes, link))
    ctx.github.add_labels(repo, number, [BUILT])
    ctx.github.remove_label(repo, number, base.IN_PROGRESS)
    log.info("implement run %s built %s#%s: landed on %s at %s", run["id"], repo, number, branch,
             head, extra=_about(run))


def _conflicted(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """The ticket's commits conflict with the integration branch: they're
    discarded (`superseded`) and the ticket is queued again from the new head
    as its next attempt; out of attempts, it's given up on."""
    with ctx.conn:
        ctx.conn.execute("UPDATE runs SET outcome = 'superseded' WHERE id = ?", (run["id"],))
    run = loop.get_run(ctx.conn, run["id"])
    assert run is not None
    log.info("implement run %s: its commits conflict with %s; discarded", run["id"],
             workflow["branch"], extra=_about(run))
    if attempts.retry(ctx.conn, run) is None:
        implement_ticket.give_up(ctx, run, workflow, again="agent:implement", stop=False)


def render_landed_comment(
    run_id: int, branch: str, head: str, pr_body: str | None, log_lines: list[str],
    notes: list[str], log_link: str,
) -> str:
    """The comment on a landed ticket: implement's `pr_body` (or the commit
    log without one), under the factory's own heading. The spec PR links to it."""
    if pr_body and pr_body.strip():
        summary = pr_body.strip()
    else:
        summary = "Commits:\n\n```\n" + "\n".join(log_lines) + "\n```"
    footer = [*notes, f"Factory run {run_id} · [runner log]({log_link})"]
    return (f"{LANDED_MARK.format(run_id)}\n## Built\n\n"
            f"Landed on `{branch}` at {head}. This ticket stays open until the spec's pull "
            f"request merges.\n\n{summary}\n\n---\n" + "\n\n".join(footer) + "\n")


def _explored(
    ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, output: dict[str, Any]
) -> None:
    """Spec explore's verdict: `needs-a-human`, `waiting` and `duplicate` stop
    the workflow as for a ticket. Otherwise (`easy-call`, or `too-big`, which
    doesn't apply: auto-tickets' ticket limit handles size) the findings are
    posted as the spec exploration notes and auto-tickets is queued."""
    if implement_ticket.explore_stopped(ctx, run, workflow, output, heading=EXPLORE_HEADING):
        return
    if ctx.conn.execute(
        "SELECT 1 FROM runs WHERE workflow_id = ? AND task = 'auto-tickets' AND id > ?",
        (run["workflow_id"], run["id"]),
    ).fetchone():
        return  # handled already, up to marking the run handled
    implement_ticket.post_explore(ctx, run, output, heading=NOTES_HEADING)
    spec = int(run["ticket"])
    if _refuse_open_tickets(ctx.github, run["repo"], spec, run["triggered_by"]):
        base.end(ctx, workflow, "stopped", spec)
        return
    _queue_auto_tickets(ctx, run, workflow)


def _queue_auto_tickets(ctx: Context, after: sqlite3.Row, workflow: sqlite3.Row) -> None:
    """Queue auto-tickets on the spec, with the notes of spec explore run
    `after` (whose `triggered_by` it copies) in its input."""
    repo, spec = after["repo"], int(after["ticket"])
    enrolment = enrolments.get(ctx.conn, repo)
    assert enrolment is not None  # an unenrolled repo's runs are never handled
    issue = ctx.github.get_issue(repo, spec)
    try:
        dispatch = tickets.fit(
            {
                "contract_version": CONTRACT_VERSION,
                "repo": repo,
                "task": "auto-tickets",
                "ticket": str(spec),
                "older_issues": tickets.older_issues(ctx.github, repo, spec),
                "base_sha": ctx.github.default_branch_head(repo),
                "enrolment_sha": enrolment.sha,
                "model": ctx.settings.model("auto-tickets", enrolment),
                "timeout_minutes": ctx.settings.timeout("auto-tickets", enrolment),
            },
            _with_notes(tickets.fetch(ctx.github, repo, issue), after),
        )
    except tickets.TooBig as e:
        ctx.github.post_comment(repo, spec,
                                tickets.render_too_big_notice(str(e), after["triggered_by"]))
        base.end(ctx, workflow, "stopped", spec)
        log.info("%s#%s is too big to dispatch: %s", repo, spec, e, extra=_about(after))
        return
    run_id = loop.enqueue(ctx.conn, repo=repo, task="auto-tickets", ticket=str(spec),
                          dispatch=dispatch, workflow_id=workflow["id"],
                          triggered_by=after["triggered_by"],
                          triggered_by_id=after["triggered_by_id"])
    log.info("queued auto-tickets run %s for %s#%s after spec explore run %s", run_id, repo,
             spec, after["id"], extra=about_run(id=run_id, repo=repo, ticket=str(spec)))


def spec_ticket(
    conn: sqlite3.Connection, github: GitHub, repo: str, issue: dict[str, Any],
    workflow: sqlite3.Row,
) -> tickets.Ticket:
    """A spec ticket (`issue`, as GitHub gives it) as its implement reads it:
    the ticket, then the spec exploration notes of `workflow`'s spec, so each
    implementer starts from them. Without notes, just the ticket."""
    ticket = tickets.fetch(github, repo, issue)
    explore = conn.execute(
        "SELECT * FROM runs WHERE workflow_id = ? AND task = 'explore' AND outcome = 'succeeded' "
        "ORDER BY id DESC LIMIT 1", (workflow["id"],)).fetchone()
    return ticket if explore is None else _with_notes(ticket, explore)


def _with_notes(ticket: tickets.Ticket, explore: sqlite3.Row) -> tickets.Ticket:
    """The spec as auto-tickets reads it: the spec exploration notes in their
    own section, and not again as the comment they were posted in."""
    mark = f"<!-- factory:explore run={explore['id']} -->"
    output = json.loads(explore["result"])["output"]
    return replace(ticket, comments=[c for c in ticket.comments if mark not in c.body],
                   sections=[*ticket.sections, (NOTES_HEADING, render_explore_sections(output))])


def prepare(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> dict[str, Any] | None:
    """Spec explore and auto-tickets read the default branch's head at
    dispatch time, so a row held back by the usage pause or a retry doesn't
    read stale code; the integration branch starts at auto-tickets' `base_sha`
    as sent. A spec ticket's implement starts from the integration branch's
    head at dispatch time, so it builds on everything landed since it was
    queued."""
    dispatch = json.loads(run["dispatch"])
    if run["task"] in ("explore", "auto-tickets"):
        return {**dispatch, "base_sha": ctx.github.default_branch_head(run["repo"])}
    if run["task"] == "implement" and workflow["branch"]:
        head = _store(ctx).branch_head(run["repo"], workflow["branch"])
        if head is None:
            raise bundles.GitError(f"{workflow['branch']} isn't on {run['repo']}")
        return {**dispatch, "base_sha": head}
    return None


# --- the build queue --------------------------------------------------------------------

BUILT = "agent:built"
_BLOCKED_BY = re.compile(r"^## Blocked by[ \t]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
_BLOCKER = re.compile(r"^\s*[-*]\s*#(\d+)\b", re.MULTILINE)


def blockers(body: str | None) -> list[int]:
    """The issue numbers a ticket's `## Blocked by` section lists (`- #N` lines)."""
    match = _BLOCKED_BY.search(body or "")
    return [int(n) for n in _BLOCKER.findall(match.group(1))] if match else []


def advance(ctx: Context) -> None:
    """Each pass, move every active spec's build queue forward (`queue`). A
    spec whose GitHub or git calls fail is tried again on the next pass."""
    rows = ctx.conn.execute(
        "SELECT * FROM workflows WHERE kind = ? AND status = 'active' AND branch IS NOT NULL "
        "ORDER BY id", (KIND,)).fetchall()
    for workflow in rows:
        if enrolments.get(ctx.conn, workflow["repo"]) is None:
            continue  # unenrolled: its workflows are stopped
        try:
            queue(ctx, workflow)
        except (GitHubError, OSError) as e:
            log.error("%s#%s: moving the build queue failed: %s", workflow["repo"],
                      workflow["subject"], e,
                      extra={"repo": workflow["repo"], "ticket": workflow["subject"]})


def queue(
    ctx: Context, workflow: sqlite3.Row, prefer: "tuple[int, Added] | None" = None
) -> list[int]:
    """Queue implement for the lowest-numbered buildable tickets of the spec,
    while fewer than `per_spec_concurrency` of its tickets have implement
    queued or running (a queued row counts: its slot is taken). Returns the
    tickets queued.

    The queue is whatever the issues say, read again each time. A ticket is
    buildable when it's open, not `agent:built`, not stuck (`needs-a-human`,
    `waiting`, `agent:blocked`), has no active run of its own, and every
    issue in its `## Blocked by` has landed: a ticket of the spec once it's
    open and `agent:built` (one closed by hand never counts), any other issue
    once it's closed. Implement credits whoever started the spec, or, for
    ticket `prefer[0]`, `prefer[1]` (who re-added `agent:implement`)."""
    repo = workflow["repo"]
    everything = _spec_tickets(ctx, workflow)
    active = ctx.conn.execute(
        "SELECT * FROM runs WHERE repo = ? AND status IN ('queued', 'dispatched')",
        (repo,)).fetchall()
    busy = {r["ticket"] for r in active}
    in_flight = len({r["ticket"] for r in active
                     if r["workflow_id"] == workflow["id"] and r["task"] == "implement"})
    landed = _landed(ctx, repo, everything)
    queued: list[int] = []
    for number in sorted(everything):
        if in_flight >= ctx.settings.per_spec_concurrency:
            break
        issue = everything[number]
        names = _labels(issue)
        if (issue["state"] != "open" or BUILT in names or str(number) in busy
                or any(label in names for label in base.STALE)):
            continue
        if not all(landed(b) for b in blockers(issue.get("body"))):
            continue
        who = prefer[1] if prefer is not None and prefer[0] == number else None
        if _queue_ticket(ctx, workflow, issue, who):
            queued.append(number)
            in_flight += 1
    return queued


def _spec_tickets(ctx: Context, workflow: sqlite3.Row) -> dict[int, dict[str, Any]]:
    """Every ticket of the workflow's spec, open or closed, by number."""
    label = spec_label(workflow["subject"])
    return {i["number"]: i for i in ctx.github.issues_with_label(workflow["repo"], label)
            if "pull_request" not in i}


def _landed(
    ctx: Context, repo: str, everything: dict[int, dict[str, Any]]
) -> Callable[[int], bool]:
    """Whether a blocker has landed: a ticket of the spec (`everything`) once
    it's open and `agent:built`, any other issue once it's closed. An issue
    that doesn't exist never has."""
    outside: dict[int, bool] = {}

    def landed(number: int) -> bool:
        if number in everything:
            issue = everything[number]
            return issue["state"] == "open" and BUILT in _labels(issue)
        if number not in outside:
            try:
                outside[number] = ctx.github.get_issue(repo, number)["state"] == "closed"
            except GitHubError as e:
                if e.status not in (404, 410):
                    raise
                outside[number] = False
        return outside[number]

    return landed


def start_ticket(
    conn: sqlite3.Connection,
    github: GitHub,
    settings: "Settings",
    issue: dict[str, Any],
    enrolment: Enrolment,
    triggered_by: "Added | None",
) -> int | None:
    """A human added `agent:implement` to a `spec:<n>` ticket (one stuck, or
    one added by hand): it's built on the spec's integration branch, not as
    a ticket of its own. Its stale status labels come off and the spec's
    workflow is set back to `active`; the queue then builds it as soon as its
    blockers have landed and a slot is free (now, if they have), crediting
    whoever added the label. Returns the spec's workflow id, or None when a
    notice says why nothing changed."""
    repo, number = enrolment.repo, issue["number"]
    spec = next(n.removeprefix("spec:") for n in _labels(issue) if n.startswith("spec:"))
    mention = [triggered_by.login] if triggered_by else None
    if triggered_by is None or triggered_by.id is None:
        github.post_comment(repo, number, render_notice(
            "The issue's events don't say who added `agent:implement`, so nothing was started. "
            "Add it again."))
        return None
    workflow = conn.execute(
        "SELECT * FROM workflows WHERE repo = ? AND kind = ? AND subject = ? "
        "AND branch IS NOT NULL ORDER BY id DESC LIMIT 1", (repo, KIND, spec)).fetchone()
    if workflow is None or workflow["status"] == "cancelled":
        why = ("isn't being built by the factory" if workflow is None
               else "had its build cancelled when its pull request was closed")
        github.post_comment(repo, number, render_notice(
            f"Spec #{spec} {why}, so this ticket has no integration branch to be built on and "
            f"nothing was started. To build the spec, close its `{spec_label(spec)}` tickets "
            f"and add `{TRIGGER}` to #{spec}.", mention))
        return None
    for label in base.STALE:
        github.remove_label(repo, number, label)
    base.start(conn, repo=repo, kind=KIND, subject=spec)
    base.mark_in_progress(github, repo, int(spec))
    workflow = base.get(conn, workflow["id"])
    assert workflow is not None
    ctx = Context(conn, github, cast(Runner, None), settings)  # queueing never uses the runner
    if number in queue(ctx, workflow, prefer=(number, triggered_by)):
        return workflow["id"]
    everything = _spec_tickets(ctx, workflow)
    landed = _landed(ctx, repo, everything)
    waiting = [b for b in blockers(issue.get("body")) if not landed(b)]
    if waiting:
        listed = ", ".join(f"#{b}" for b in waiting)
        why = (f"once {listed} {'has' if len(waiting) == 1 else 'have'} landed on "
               f"`{workflow['branch']}`")
    else:
        why = "as soon as the spec has a free slot"
    github.post_comment(repo, number, render_notice(
        f"This ticket will be built on spec #{spec}'s integration branch {why}.", mention))
    log.info("%s#%s, a ticket of spec #%s, waits to be built", repo, number, spec,
             extra={"repo": repo, "ticket": str(number)})
    return workflow["id"]


def _queue_ticket(
    ctx: Context, workflow: sqlite3.Row, issue: dict[str, Any], who: "Added | None"
) -> bool:
    """Queue implement for spec ticket `issue` on the integration branch and
    mark it in progress. False if it's too big to dispatch: a notice, and the
    ticket is labelled `needs-a-human` so it isn't tried again every pass."""
    repo, number = workflow["repo"], issue["number"]
    if who is not None and who.id is not None:
        credit = (who.login, who.id)
    else:
        started = ctx.conn.execute(
            "SELECT triggered_by, triggered_by_id FROM runs WHERE workflow_id = ? "
            "AND task = 'explore' AND triggered_by_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (workflow["id"],)).fetchone()
        assert started is not None  # spec explore made the branch's workflow go on
        credit = (started["triggered_by"], started["triggered_by_id"])
    head = _store(ctx).branch_head(repo, workflow["branch"])
    if head is None:
        raise bundles.GitError(f"{workflow['branch']} isn't on {repo}")
    try:
        run_id = implement_ticket.enqueue_implement(
            ctx.conn, ctx.github, ctx.settings, issue, repo, workflow["id"], credit,
            ticket_md=spec_ticket(ctx.conn, ctx.github, repo, issue, workflow), base_sha=head)
    except tickets.TooBig as e:
        ctx.github.post_comment(repo, number, tickets.render_too_big_notice(str(e), credit[0]))
        ctx.github.add_labels(repo, number, ["needs-a-human"])
        log.info("%s#%s is too big to dispatch: %s", repo, number, e,
                 extra={"repo": repo, "ticket": str(number)})
        return False
    base.mark_in_progress(ctx.github, repo, number)
    log.info("queued implement run %s for %s#%s, a ticket of spec #%s", run_id, repo, number,
             workflow["subject"], extra=about_run(id=run_id, repo=repo, ticket=str(number)))
    return True


def _labels(issue: dict[str, Any]) -> list[str]:
    return [label["name"] if isinstance(label, dict) else label
            for label in issue.get("labels", [])]


def _store(ctx: Context) -> bundles.Store:
    return bundles.Store(ctx.settings.git_dir, ctx.github.git_remote)


def integration_branch(spec: int | str) -> str:
    return f"factory/spec-{spec}"


def body_hash(body: str | None) -> str:
    """The `spec_hash` of a spec's body: an edit changes it, labels and
    comments don't (orchestrator.md#implement-spec)."""
    return hashlib.sha256((body or "").encode()).hexdigest()


def _decomposed(
    ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row, breakdown: list[dict[str, Any]]
) -> None:
    """One issue per ticket, in order, then the integration branch, then a
    notice on the spec.

    Crash-safe: each body carries the run and its index, so after a restart
    the tickets this run already made are found (open or closed) and skipped.
    Blockers come first (checked when the result was collected, see
    `loop._judge`), so every `blocked_by` index has its issue number by the
    time a ticket names it. Creating the branch again is a no-op.
    """
    repo, spec = run["repo"], int(run["ticket"])
    label = spec_label(spec)
    ctx.github.create_label(repo, label, SPEC_LABEL_COLOR, f"A ticket of spec #{spec}")
    numbers = made_by(ctx.github.issues_with_label(repo, label), run["id"])
    for index, ticket in enumerate(breakdown):
        if index in numbers:
            continue
        blockers = [numbers[i] for i in ticket["blocked_by"]]
        body = render_ticket(run["id"], index, spec, ticket, blockers)
        numbers[index] = ctx.github.create_issue(repo, ticket["title"], body, [label])
        log.info("auto-tickets run %s: created %s#%s (ticket %s of %s)", run["id"], repo,
                 numbers[index], index + 1, len(breakdown), extra=_about(run))
    if not _create_integration_branch(ctx, run, workflow):
        return
    listed = "\n".join(f"{i + 1}. #{numbers[i]}" for i in range(len(breakdown)))
    ctx.github.post_comment(repo, spec, render_notice(
        f"Auto-tickets (factory run {run['id']}) broke this spec into {len(breakdown)} "
        f"ticket{'s' if len(breakdown) != 1 else ''}, labelled `{label}`:\n\n{listed}\n\n"
        f"They're built in order on the spec's integration branch, "
        f"`{integration_branch(spec)}`. Edit them at any time: "
        "the queue re-reads them on every poll."))


def _create_integration_branch(ctx: Context, run: sqlite3.Row, workflow: sqlite3.Row) -> bool:
    """Create `factory/spec-<n>` at auto-tickets' `base_sha` (the code it read
    to write the breakdown) and store it, with the spec body's hash, in the
    workflow's row. If the branch is already somewhere else, nothing is
    forced: a notice asks a human to move it out of the way, the workflow
    stops, and False."""
    repo, spec = run["repo"], int(run["ticket"])
    branch = integration_branch(spec)
    base_sha = json.loads(run["dispatch"])["base_sha"]
    try:
        bundles.Store(ctx.settings.git_dir, ctx.github.git_remote).create_branch(
            repo, branch, base_sha)
    except bundles.Moved:
        ctx.github.post_comment(repo, spec, render_notice(
            f"The integration branch `{branch}` already exists, from an earlier start, so "
            "nothing is built (the factory never force-pushes). Delete or rename it, close "
            f"the `{spec_label(spec)}` tickets, and add `agent:implement-spec` again.",
            [run["triggered_by"]] if run["triggered_by"] else None))
        base.end(ctx, workflow, "stopped", spec, "needs-a-human")
        log.info("%s#%s: %s already exists elsewhere; workflow stopped", repo, spec, branch,
                 extra=_about(run))
        return False
    spec_hash = body_hash(ctx.github.get_issue(repo, spec).get("body"))
    with ctx.conn:
        ctx.conn.execute("UPDATE workflows SET branch = ?, spec_hash = ? WHERE id = ?",
                         (branch, spec_hash, workflow["id"]))
    log.info("auto-tickets run %s: created %s at %s for %s#%s", run["id"], branch, base_sha,
             repo, spec, extra=_about(run))
    return True


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
