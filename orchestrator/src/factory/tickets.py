"""Tickets as a task reads them: `ticket.md`, rendered by the orchestrator.

`fetch` reads an issue from GitHub with the comments a task should see:
those by people with write access and the App's own, minus the factory's
status comments and notices, and any history of being closed as a duplicate
and reopened. `render` turns it into markdown, and `fit` puts that markdown in
a dispatch, trimming until the dispatch fits GitHub's input cap.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from factory.github import GitHub

EXPLORE_MARK = "<!-- factory:explore "
# The heading workflows.implement_ticket.render_explore_comment writes for a duplicate.
DUPLICATE_OF = re.compile(r"^### Duplicate of\n\n#(\S+)$", re.MULTILINE)
STATUS_MARK = "<!-- factory:status -->"
NOTICE_MARK = "<!-- factory:notice -->"
WRITERS = ("admin", "write")
OLDER_ISSUES = 200  # how many older open issues explore gets, for spotting duplicates
# GitHub caps workflow_dispatch inputs at 65,535 characters; aim lower for a margin.
DISPATCH_TARGET = 64_000
RUN_ID_ROOM = 2**31 - 1


@dataclass(frozen=True)
class Comment:
    author: str
    body: str
    created_at: str  # ISO-8601, as GitHub gives it


@dataclass(frozen=True)
class Ticket:
    number: int
    title: str
    body: str
    comments: list[Comment] = field(default_factory=list)  # oldest first
    history: list[str] = field(default_factory=list)  # e.g. a reopened duplicate


def fetch(github: GitHub, repo: str, issue: dict[str, Any]) -> Ticket:
    """`issue` (as GitHub lists it) with the comments a task should see."""
    app = github.app_login()
    permissions: dict[str, str] = {}

    def kept(comment: dict[str, Any]) -> bool:
        login = comment["user"]["login"]
        body = comment.get("body") or ""
        if login == app:
            return STATUS_MARK not in body and NOTICE_MARK not in body
        if login not in permissions:
            permissions[login] = github.permission(repo, login)
        return permissions[login] in WRITERS

    raw = github.issue_comments(repo, issue["number"])
    comments = [Comment(c["user"]["login"], c.get("body") or "", c["created_at"]) for c in raw if kept(c)]
    history = _duplicate_history(github.issue_events(repo, issue["number"]), raw, app)
    return Ticket(issue["number"], issue["title"], issue.get("body") or "", comments, history)


def _duplicate_history(
    events: list[dict[str, Any]], comments: list[dict[str, Any]], app: str
) -> list[str]:
    """One line per time the factory closed the ticket as a duplicate and a human reopened it.

    The duplicate is the one named by the App's latest explore findings before the close.
    """
    history = []
    closed: tuple[str, str] | None = None  # (date, duplicate) of the factory's close
    for event in events:
        actor = (event.get("actor") or {}).get("login")
        if event["event"] == "closed":
            closed = None
            if actor == app:
                duplicate = _named_duplicate(comments, app, before=event["created_at"])
                if duplicate:
                    closed = (event["created_at"][:10], duplicate)
        elif event["event"] == "reopened" and closed and actor != app:
            history.append(
                f"Closed by the factory as a duplicate of #{closed[1]} on {closed[0]}; "
                f"reopened by @{actor}."
            )
            closed = None
    return history


def _named_duplicate(comments: list[dict[str, Any]], app: str, *, before: str) -> str | None:
    for c in reversed(comments):
        body = c.get("body") or ""
        if c["user"]["login"] == app and c["created_at"] <= before and EXPLORE_MARK in body:
            match = DUPLICATE_OF.search(body)
            return match.group(1) if match else None
    return None


def older_issues(github: GitHub, repo: str, number: int, limit: int = OLDER_ISSUES) -> list[dict[str, str]]:
    """The `limit` most recent open issues older than issue `number`, newest first.

    Issue numbers grow with creation, so older means a lower number. PRs are left out.
    """
    older: list[dict[str, str]] = []
    if limit <= 0:
        return older
    for issue in github.open_issues_newest_first(repo):
        if "pull_request" in issue or issue["number"] >= number:
            continue
        older.append({"ticket": str(issue["number"]), "title": issue["title"]})
        if len(older) == limit:
            break
    return older


class TooBig(Exception):
    """The ticket's title and body alone don't fit in a dispatch."""


def dispatch_size(dispatch: dict[str, Any]) -> int:
    """What GitHub measures against its 65,535 cap: the `inputs` object as JSON.

    The dispatch is itself a JSON string inside it, so each `"`, `\\` and
    newline in the dispatch's JSON counts twice.
    """
    return len(json.dumps({"dispatch": json.dumps(dispatch)}))


def fit(dispatch: dict[str, Any], ticket: Ticket, *, target: int = DISPATCH_TARGET) -> dict[str, Any]:
    """`dispatch` (without `run_id`) with `ticket_markdown` rendered from `ticket`, made to fit.

    Over `target`, it trims, in order: the older issues (newest kept), the
    oldest comments, then `previous_attempt`'s check output (its end kept),
    and says so at the top of `ticket.md`. The title and body are never cut:
    if they don't fit with everything else trimmed, it raises `TooBig`.
    """
    issues = dispatch.get("older_issues") or []
    attempt = dispatch.get("previous_attempt") or {}
    output = attempt.get("check_output") or ""
    n_comments = len(ticket.comments)

    def build(keep_issues: int, keep_comments: int, keep_output: int) -> dict[str, Any]:
        built = dict(dispatch)
        notes = []
        if keep_issues < len(issues):
            built["older_issues"] = issues[:keep_issues]
            notes.append(f"{len(issues) - keep_issues} older issues omitted")
        if keep_comments < n_comments:
            notes.append(f"{n_comments - keep_comments} older comments omitted")
        if keep_output < len(output):
            built["previous_attempt"] = {k: v for k, v in attempt.items() if k != "check_output"}
            if keep_output:
                built["previous_attempt"]["check_output"] = output[len(output) - keep_output :]
                notes.append("the previous attempt's check output shortened")
            else:
                notes.append("the previous attempt's check output omitted")
        kept = replace(ticket, comments=ticket.comments[n_comments - keep_comments :])
        built["ticket_markdown"] = render(kept, notes)
        return built

    def fits(built: dict[str, Any]) -> bool:
        # Measured with the largest run_id we'd expect, since it's added on enqueue.
        return dispatch_size({"run_id": RUN_ID_ROOM, **built}) <= target

    stages = [
        lambda k: build(k, n_comments, len(output)),
        lambda k: build(0, k, len(output)),
        lambda k: build(0, 0, k),
    ]
    tops = [len(issues), n_comments, len(output)]
    if fits(whole := stages[0](tops[0])):
        return whole
    for stage, top in zip(stages, tops, strict=True):
        if fits(stage(0)):
            return stage(_largest(top, lambda k: fits(stage(k))))
    raise TooBig(
        f"the ticket's title and body make a dispatch of {dispatch_size(stages[-1](0)):,} "
        f"characters; the most is {target:,}"
    )


def _largest(top: int, ok: Callable[[int], bool]) -> int:
    """The largest k in [0, top] with ok(k), given ok(0) and that ok only turns false as k grows."""
    lo, hi = 0, top
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def render(ticket: Ticket, trimmed: Sequence[str] = ()) -> str:
    """`ticket.md`: a note of anything trimmed, the title, the body, history, then
    the comments, oldest first."""
    parts = [f"> Trimmed to fit the dispatch: {', '.join(trimmed)}.\n"] if trimmed else []
    parts.append(f"# {ticket.title}\n")
    if ticket.body.strip():
        parts.append(f"{ticket.body.strip()}\n")
    if ticket.history:
        parts.append("## History\n")
        parts.append("".join(f"- {line}\n" for line in ticket.history))
    if ticket.comments:
        parts.append("## Comments\n")
        for c in ticket.comments:
            parts.append(f"### @{c.author} on {c.created_at[:10]}\n")
            parts.append(f"{c.body.strip()}\n")
    return "\n".join(parts)


def render_notice(message: str, mention: list[str] | None = None) -> str:
    """A notice: a new comment, marked, @mentioning who has to act."""
    who = "".join(f"@{login} " for login in mention or [])
    return f"{NOTICE_MARK}\n{who}{message}\n"


def render_too_big_notice(reason: str, mention: str | None = None) -> str:
    """The notice for a ticket whose title and body alone don't fit in a dispatch.

    `mention` is the login that triggered the run.
    """
    return render_notice(
        f"This ticket's title and body are too long to dispatch ({reason}). "
        "Please shorten it, for example by moving logs or long examples into a linked "
        "file or gist, then add the label again.",
        [mention] if mention else [],
    )
