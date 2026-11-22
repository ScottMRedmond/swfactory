"""What implement-spec writes to GitHub: the spec PR's body, the comment on
a landed ticket, and the issue body of each ticket auto-tickets made (with
the marker `tickets_made_by` finds again after a crash).
"""

import re
from typing import Any

from factory.github import label_names
from factory.workflows import base, implement_ticket
from factory.workflows.spec_stall import BUILT, STUCK

LANDED_MARK = "<!-- factory:landed run={} -->"
_MARKER = re.compile(r"<!-- factory:auto-tickets run=(\d+) index=(\d+) -->")


def render_spec_pr_body(
    spec: int, branch: str, all_tickets: dict[int, dict[str, Any]], links: dict[int, str | None]
) -> str:
    """The spec PR's body: each ticket of the spec with its status, a landed
    one linking to its landing comment, then a footer closing the spec and
    every ticket but those a human closed by hand (orchestrator.md#implement-spec)."""
    lines = []
    for number in sorted(all_tickets):
        issue = all_tickets[number]
        names = label_names(issue)
        if issue["state"] != "open":
            status = "closed by hand"
        elif BUILT in names:
            link = links.get(number)
            status = f"landed ([landing comment]({link}))" if link else "landed"
        elif stuck := next((label for label in base.STALE if label in names), None):
            status = STUCK[stuck]
        elif base.IN_PROGRESS in names:
            status = "building"
        else:
            status = "not built yet"
        lines.append(f"- #{number} {issue['title']}: {status}")
    closes = [spec, *(n for n in sorted(all_tickets) if all_tickets[n]["state"] == "open")]
    footer = ["\n".join(f"Closes #{n}" for n in closes),
              f"Built ticket by ticket on `{branch}`. "
              + implement_ticket.DRAFT_NOTICE]
    return (f"The tickets of spec #{spec}:\n\n" + "\n".join(lines)
            + "\n\n---\n" + "\n\n".join(footer) + "\n")


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


def tickets_made_by(issues: list[dict[str, Any]], run_id: int) -> dict[int, int]:
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
