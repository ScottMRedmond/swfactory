"""Why a spec's tickets can't move (orchestrator.md#implement-spec, stalls).

`blockers` reads a ticket's `## Blocked by` list; `Landed` says whether a
blocker has landed; `stall`, called when nothing is buildable or in flight,
names what holds each ticket back, or None if one can still move by itself.
"""

import re
from typing import Any

from factory.github import GitHubError, label_names
from factory.workflows import base
from factory.workflows.base import Context

BUILT = "agent:built"
_BLOCKED_BY = re.compile(r"^## Blocked by[ \t]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
_BLOCKER = re.compile(r"^\s*[-*]\s*#(\d+)\b", re.MULTILINE)
# How a ticket's stale status label reads in the spec PR and a stall notice.
STUCK = {"needs-a-human": "needs a human", "waiting": "waiting", "agent:blocked": "blocked"}


def blockers(body: str | None) -> list[int]:
    """The issue numbers a ticket's `## Blocked by` section lists (`- #N` lines)."""
    match = _BLOCKED_BY.search(body or "")
    return [int(n) for n in _BLOCKER.findall(match.group(1))] if match else []


class Landed:
    """Whether a blocker has landed: a ticket of the spec (`all_tickets`) once
    it's open and `agent:built`, any other issue once it's closed. An issue
    that doesn't exist never has; it's in `missing` once asked about."""

    def __init__(self, ctx: Context, repo: str, all_tickets: dict[int, dict[str, Any]]):
        self._ctx, self._repo, self._tickets = ctx, repo, all_tickets
        self._outside: dict[int, bool] = {}
        self.missing: set[int] = set()

    def __call__(self, number: int) -> bool:
        if number in self._tickets:
            issue = self._tickets[number]
            return issue["state"] == "open" and BUILT in label_names(issue)
        if number not in self._outside:
            try:
                state = self._ctx.github.get_issue(self._repo, number)["state"]
                self._outside[number] = state == "closed"
            except GitHubError as e:
                if e.status not in (404, 410):
                    raise
                self._outside[number] = False
                self.missing.add(number)
        return self._outside[number]


def stall(all_tickets: dict[int, dict[str, Any]], landed: Landed) -> list[str] | None:
    """Why the spec's tickets can't move, one line per ticket (or cycle), or
    None if one of them still can by itself: it waits only on an open issue
    outside the spec. Called when nothing is buildable or in flight. A ticket
    is stuck by its label (`needs-a-human`, `waiting`, `agent:blocked`), a
    blocker closed by hand or that doesn't exist, a cycle, or a stuck blocker."""
    unbuilt = {n: i for n, i in all_tickets.items()
               if i["state"] == "open" and BUILT not in label_names(i)}
    own: dict[int, str] = {}  # what holds a ticket back by itself
    deps: dict[int, list[int]] = {}  # its blockers among the unbuilt tickets
    outside: dict[int, list[int]] = {}  # its open blockers outside the spec
    for number, issue in unbuilt.items():
        names = label_names(issue)
        deps[number], outside[number] = [], []
        reasons = [STUCK[label] for label in base.STALE if label in names]
        for b in blockers(issue.get("body")):
            if landed(b):
                continue
            if b in unbuilt:
                deps[number].append(b)
            elif b in all_tickets and all_tickets[b]["state"] != "open":
                reasons.append(f"blocked by #{b}, which was closed by hand")
            elif b in all_tickets:
                pass  # built: landed
            elif b in landed.missing:
                reasons.append(f"blocked by #{b}, which doesn't exist")
            else:
                outside[number].append(b)
        if reasons:
            own[number] = "; ".join(reasons)

    def reaches(start: int, goal: int) -> bool:
        seen, todo = set(), list(deps[start])
        while todo:
            n = todo.pop()
            if n == goal:
                return True
            if n not in seen:
                seen.add(n)
                todo.extend(deps[n])
        return False

    cyclic = {n for n in unbuilt if reaches(n, n)}
    moving: dict[int, bool] = {}

    def can_move(n: int) -> bool:
        # Only waiting on outside issues, through tickets that can move too.
        if n not in moving:
            moving[n] = False  # guards a cycle while it's worked out
            moving[n] = (n not in own and n not in cyclic
                         and all(can_move(d) for d in deps[n]))
        return moving[n]

    if any(can_move(n) for n in unbuilt):
        return None
    lines = [f"- #{n} {unbuilt[n]['title']}: {own[n]}" for n in sorted(own)]
    if cyclic:
        listed = ", ".join(f"#{n}" for n in sorted(cyclic))
        lines.append(f"- {listed}: each waits on another in a cycle of `## Blocked by` references")
    for n in sorted(unbuilt):
        if n not in own and n not in cyclic:
            waits = ", ".join(f"#{d}" for d in sorted(deps[n]))
            lines.append(f"- #{n} {unbuilt[n]['title']}: waits on {waits}")
    if not unbuilt:
        lines = [f"- #{n} {i['title']}: closed by hand" for n, i in sorted(all_tickets.items())
                 if i["state"] != "open"]
    return lines
