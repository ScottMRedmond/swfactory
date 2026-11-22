"""Review: what the orchestrator sends a review and does with its result
(orchestrator.md#review), whichever workflow (or none) the PR belongs to.

- `against` decides what a PR is reviewed against: the ticket of the factory
  workflow that opened it (a spec PR: the spec and its built tickets), else
  the issues it closes, else nothing (`no-ticket`).
- `render_input` makes review's `ticket.md`: the PR, what it's reviewed
  against, and the PR's comments filtered as for tickets.
- `publish` posts a result: the findings on a changed line inline, as one
  `COMMENT` review (`post_findings`), then the verdict comment with the rest.
- `render_verdict` is the one verdict comment; `post_verdict` posts it (once)
  and minimizes the previous one as outdated; `finish` marks the PR ready,
  labels it and asks its human to review it.
- `is_fork` tells a fork PR, which is never reviewed.
"""

import json
import logging
import re
import sqlite3
from dataclasses import replace
from typing import Any

from factory import tickets
from factory.github import GitHub

log = logging.getLogger(__name__)

MARK = "<!-- factory:review run={} -->"
_MARK_PREFIX = "<!-- factory:review run="
FINDINGS_MARK = "<!-- factory:review-findings run={} -->"
# At most this many findings go inline (the output's `maxItems`); the rest go
# into the verdict comment.
MAX_INLINE = 25
INSTRUCTIONS_CHANGED = ("⚠️ This PR changes agent instructions (`CLAUDE.md`, `AGENTS.md` or "
                        "`.claude/`); it was reviewed against the base's.")


def against(conn: sqlite3.Connection, github: GitHub, repo: str, pr: int) -> list[int]:
    """The issues PR `pr` is reviewed against. A PR the factory opened for a
    ticket is reviewed against that ticket, from the factory's own state; a
    spec PR against the spec and every open ticket of it built on the
    integration branch (one closed by hand isn't); any other against the
    issues it closes. Empty means `no-ticket`."""
    row = conn.execute(
        "SELECT kind, subject FROM workflows WHERE repo = ? "
        "AND kind IN ('implement-ticket', 'implement-spec') AND pr = ? ORDER BY id DESC LIMIT 1",
        (repo, pr),
    ).fetchone()
    if row is not None and row["kind"] == "implement-spec":
        spec = int(row["subject"])
        built = sorted(i["number"] for i in github.issues_with_label(repo, f"spec:{spec}")
                       if "pull_request" not in i and i["state"] == "open"
                       and "agent:built" in _labels(i))
        return [spec, *built]
    if row is not None:
        return [int(row["subject"])]
    return github.closing_issues(repo, pr)


def _labels(issue: dict[str, Any]) -> list[str]:
    return [label["name"] if isinstance(label, dict) else label
            for label in issue.get("labels", [])]


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


def is_fork(pull: dict[str, Any], repo: str) -> bool:
    """Whether PR `pull` (as `GitHub.pull` gives it) comes from another repo
    than `repo`. A deleted fork's head has no repo at all."""
    head_repo = (pull.get("head") or {}).get("repo")
    return head_repo is None or str(head_repo.get("full_name", "")).lower() != repo.lower()


def changed_lines(patch: str | None) -> set[int]:
    """The new-side line numbers a file's patch adds (not its context lines)."""
    lines: set[int] = set()
    n = 0
    for text in (patch or "").splitlines():
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", text)
        if hunk:
            n = int(hunk.group(1))
        elif text.startswith("+"):
            lines.add(n)
            n += 1
        elif text.startswith(" "):
            n += 1
    return lines


def place(
    comments: list[dict[str, Any]], files: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into those that go inline (on a changed line of the PR's
    diff, the first `MAX_INLINE`) and the rest, each in their order."""
    changed = {f["filename"]: changed_lines(f.get("patch")) for f in files}
    inline: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for c in comments:
        on_diff = c["line"] in changed.get(c["path"], set())
        (inline if on_diff and len(inline) < MAX_INLINE else rest).append(c)
    return inline, rest


def post_findings(
    github: GitHub, repo: str, pr: int, run_id: int, comments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Post run `run_id`'s findings that are on a changed line of PR `pr` as
    one `COMMENT` review, once. Returns the findings left for the verdict
    comment: all of them if GitHub rejects the review, since a misplaced
    finding never fails the run."""
    if not comments:
        return []
    pull = github.pull(repo, pr)
    inline, rest = place(comments, github.pull_files(repo, pr))
    if not inline:
        return rest
    mark = FINDINGS_MARK.format(run_id)
    app = github.app_login()
    if any(r["user"]["login"] == app and (r.get("body") or "").startswith(mark)
           for r in github.reviews(repo, pr)):
        return rest  # posted before a crash
    body = (f"{mark}\nReview's findings on the diff, from factory run {run_id}. "
            "The verdict is in the PR's comments.")
    posted = github.comment_review(
        repo, pr, body=body, commit_id=(pull.get("head") or {}).get("sha"),
        comments=[{"path": c["path"], "line": c["line"], "side": "RIGHT", "body": _inline(c)}
                  for c in inline])
    if not posted:
        log.warning("GitHub refused run %s's inline findings on %s#%s; they go in the verdict",
                    run_id, repo, pr, extra={"repo": repo, "ticket": str(pr)})
        return comments
    return rest


def _inline(c: dict[str, Any]) -> str:
    axis = "Standards" if c["axis"] == "standards" else "Spec"
    return f"**{c['title'].strip()}** ({axis})\n\n{c['body'].strip()}"


def publish(
    github: GitHub, repo: str, pr: int, run_id: int, result: dict[str, Any], log_link: str,
    *, discarded: bool = False,
) -> None:
    """Post a review's result on PR `pr`: its findings on changed lines
    inline, then the verdict comment with the rest. Called after any fixes
    are pushed, so the findings are checked against the diff as it is."""
    comments = result["output"]["comments"]
    rest = post_findings(github, repo, pr, run_id, comments)
    post_verdict(github, repo, pr, run_id, render_verdict(
        run_id, result, log_link, discarded=discarded, findings=rest,
        inline=len(comments) - len(rest)))


def render_verdict(
    run_id: int, result: dict[str, Any], log_link: str, *, discarded: bool = False,
    notes: list[str] | None = None, findings: list[dict[str, Any]] | None = None,
    inline: int = 0,
) -> str:
    """The verdict comment: the check-failed preamble when the check failed
    before the agent ran, the orchestrator's own lines, then a `Standards`
    and a `Spec` section (verdict, reasoning, unfixed findings), `Fixed` and
    `Missing rules`. `discarded` says the fixes failed the check and weren't
    pushed. `findings` are the unfixed findings to list (all, by default);
    `inline` says how many were posted inline instead."""
    output = result["output"]
    if findings is None:
        findings = output["comments"]
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
    if inline:
        parts.append(f"{inline} finding{' is' if inline == 1 else 's are'} inline on the diff.\n")
    for axis, heading in (("standards", "Standards"), ("spec", "Spec")):
        parts.append(f"### {heading}: `{output[axis]['verdict']}`\n\n"
                     f"{output[axis]['reasoning'].strip()}\n")
        listed = [c for c in findings if c["axis"] == axis]
        if listed:
            parts.append("\n".join(_finding(c) for c in listed) + "\n")
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


def output_used(run: sqlite3.Row) -> bool:
    """Whether a done review's output is acted on: it succeeded, or only its
    fixes failed the check, the one case a failed result's output is used.
    That case isn't retried: another review would cost a full run to produce
    findings it already has."""
    if run["task"] != "review" or run["result"] is None:
        return False
    if run["outcome"] == "succeeded":
        return True
    result = json.loads(run["result"])
    return (run["outcome"] == "failed" and result.get("failure") == "check_failed"
            and result.get("output") is not None)


def changes_requested(output: dict[str, Any]) -> bool:
    """Either axis asks for changes (`no-ticket` doesn't)."""
    return "changes-requested" in (output["standards"]["verdict"], output["spec"]["verdict"])
