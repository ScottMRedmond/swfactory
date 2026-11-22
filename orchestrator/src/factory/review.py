"""Review: what the orchestrator sends a review and does with its result
(orchestrator.md#review), whichever workflow (or none) the PR belongs to.

- `against` decides what a PR is reviewed against: the ticket of the factory
  workflow that opened it, else the issues it closes, else nothing (`no-ticket`).
- `render_input` makes review's `ticket.md`: the PR, what it's reviewed
  against, and the PR's comments filtered as for tickets.
- `render_verdict` is the one verdict comment; `post_verdict` posts it (once)
  and minimizes the previous one as outdated; `finish` marks the PR ready,
  labels it and asks its human to review it.
"""

import logging
import sqlite3
from dataclasses import replace
from typing import Any

from factory import tickets
from factory.github import GitHub

log = logging.getLogger(__name__)

MARK = "<!-- factory:review run={} -->"
_MARK_PREFIX = "<!-- factory:review run="
INSTRUCTIONS_CHANGED = ("⚠️ This PR changes agent instructions (`CLAUDE.md`, `AGENTS.md` or "
                        "`.claude/`); it was reviewed against the base's.")


def against(conn: sqlite3.Connection, github: GitHub, repo: str, pr: int) -> list[int]:
    """The issues PR `pr` is reviewed against. A PR the factory opened for a
    ticket is reviewed against that ticket, from the factory's own state;
    any other against the issues it closes. Empty means `no-ticket`."""
    row = conn.execute(
        "SELECT subject FROM workflows WHERE repo = ? AND kind = 'implement-ticket' AND pr = ? "
        "ORDER BY id DESC LIMIT 1",
        (repo, pr),
    ).fetchone()
    if row is not None:
        return [int(row["subject"])]
    return github.closing_issues(repo, pr)


def render_input(
    github: GitHub, repo: str, pr: dict[str, Any], specs: list[dict[str, Any]]
) -> tickets.Ticket:
    """Review's ticket: the PR (as an issue), each issue it's reviewed against,
    and the PR's comments. Only the comments are ever trimmed."""
    parts = [(pr.get("body") or "").strip()]
    if not specs:
        parts.append("## Reviewed against: no ticket\n\nThis PR closes no issue, so there's "
                     "no spec to review it against: the spec axis is `no-ticket`.")
    for spec in specs:
        text = (spec.get("body") or "").strip()
        parts.append(f"## Reviewed against: #{spec['number']} {spec['title']}"
                     + (f"\n\n{text}" if text else ""))
    fetched = tickets.fetch(github, repo, pr)
    return replace(fetched, title=f"PR #{pr['number']}: {pr['title']}",
                   body="\n\n".join(p for p in parts if p), history=[])


def render_verdict(
    run_id: int, result: dict[str, Any], log_link: str, *, discarded: bool = False,
    notes: list[str] | None = None,
) -> str:
    """The verdict comment: the check-failed preamble when the check failed
    before the agent ran, the orchestrator's own lines, then a `Standards`
    and a `Spec` section (verdict, reasoning, unfixed findings), `Fixed` and
    `Missing rules`. `discarded` says the fixes failed the check and weren't pushed."""
    output = result["output"]
    parts = [f"{MARK.format(run_id)}\n## Review\n"]
    before = result.get("check_before")
    if before is not None and not before["passed"]:
        parts.append("⚠️ The check failed before review started. The last lines of its output:\n\n"
                     + _code(before["output_tail"]))
    if discarded:
        after = result.get("check_after") or {"output_tail": ""}
        parts.append("⚠️ Review's fixes failed the check, so they were discarded: nothing was "
                     "pushed. The findings and verdict below still stand. The last lines of "
                     "the check's output:\n\n" + _code(after["output_tail"]))
    if result.get("instructions_changed"):
        parts.append(INSTRUCTIONS_CHANGED + "\n")
    parts.extend(f"{note}\n" for note in notes or [])
    for axis, heading in (("standards", "Standards"), ("spec", "Spec")):
        parts.append(f"### {heading}: `{output[axis]['verdict']}`\n\n"
                     f"{output[axis]['reasoning'].strip()}\n")
        findings = [c for c in output["comments"] if c["axis"] == axis]
        if findings:
            parts.append("\n".join(_finding(c) for c in findings) + "\n")
    fixed = output["fixed"]
    heading = "Fixed (discarded)" if discarded else "Fixed"
    parts.append(f"### {heading}\n\n" + ("\n".join(f"- {f}" for f in fixed) if fixed
                                        else "Nothing.") + "\n")
    if output.get("missing_rules"):
        parts.append("### Missing rules\n\n" + "\n".join(f"- {r}" for r in output["missing_rules"])
                     + "\n")
    parts.append(f"---\nFactory run {run_id} · [runner log]({log_link})\n")
    return "\n".join(parts)


def _finding(c: dict[str, Any]) -> str:
    body = c["body"].strip().replace("\n", "\n  ")
    return f"- **{c['title'].strip()}** (`{c['path']}:{c['line']}`)\n  {body}"


def _code(text: str) -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}\n{text.rstrip()}\n{fence}\n"


def post_verdict(github: GitHub, repo: str, pr: int, run_id: int, body: str) -> None:
    """Post run `run_id`'s verdict on PR `pr`, unless it's already there (a
    crash after posting), and minimize the previous verdict as outdated."""
    app = github.app_login()
    verdicts = [c for c in github.issue_comments(repo, pr)
                if c["user"]["login"] == app and (c.get("body") or "").startswith(_MARK_PREFIX)]
    mine = MARK.format(run_id)
    earlier = [c for c in verdicts if not c["body"].startswith(mine)]
    if not any(c["body"].startswith(mine) for c in verdicts):
        github.post_comment(repo, pr, body)
    if earlier:
        github.minimize_comment(repo, earlier[-1]["node_id"])


def finish(github: GitHub, repo: str, pr: int, *, needs_a_human: bool, reviewer: str | None) -> None:
    """Mark the PR ready (whatever the verdict: someone has read it), label it
    `needs-a-human` if a decision is left, and ask `reviewer` to review it."""
    github.mark_ready(repo, pr)
    if needs_a_human:
        github.add_labels(repo, pr, ["needs-a-human"])
    if reviewer and not github.request_review(repo, pr, reviewer):
        log.warning("couldn't request a review of %s#%s from %s", repo, pr, reviewer,
                    extra={"repo": repo, "ticket": str(pr)})


def changes_requested(output: dict[str, Any]) -> bool:
    """Either axis asks for changes (`no-ticket` doesn't)."""
    return "changes-requested" in (output["standards"]["verdict"], output["spec"]["verdict"])
