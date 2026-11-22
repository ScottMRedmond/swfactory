"""Checking a trigger label before anything is queued (orchestrator.md#triggers).

`check` looks at one issue or PR a poll found and decides what its trigger
labels mean: nothing to do, a trigger to queue, or a rejection. A rejected
label is removed with a notice that @mentions whoever added it and says what
to do instead. A label the App added itself is removed without a notice.

The label permission is the access check: only people with triage access or
higher can add labels, so the orchestrator doesn't check again. It records who
added the label, from the issue's events, as `triggered_by`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from factory.enrolment import Enrolment
from factory.github import GitHub

# The trigger labels and the task (or workflow) each starts.
LABELS = {
    "agent:explore": "explore",
    "agent:implement": "implement",
    "agent:review": "review",
    "agent:implement-spec": "implement-spec",
}
# The agent tasks each label needs enabled in the enrolment: its own task, or
# for implement-spec, every task of the workflow.
NEEDS = {
    "agent:explore": ["explore"],
    "agent:implement": ["implement"],
    "agent:review": ["review"],
    "agent:implement-spec": ["explore", "auto-tickets", "implement", "review"],
}

# Which label goes where, for notices about a wrong or conflicting label.
WHERE = (
    "`agent:explore` and `agent:implement` go on a ticket (an issue), "
    "`agent:review` goes on a pull request, "
    "and `agent:implement-spec` goes on a spec issue."
)


@dataclass(frozen=True)
class Trigger:
    """An accepted trigger: queue `task` for the issue, then remove `label`."""

    label: str
    task: str
    triggered_by: str | None


@dataclass(frozen=True)
class Rejection:
    """Remove `labels` and queue nothing. With a `message`, post it as a notice
    @mentioning `mention`; without one, remove them quietly."""

    labels: list[str]
    message: str | None = None
    mention: list[str] = field(default_factory=list)


def check(
    github: GitHub,
    repo: str,
    issue: dict[str, Any],
    enrolment: Enrolment,
    *,
    active: Sequence[Mapping[str, Any]] = (),
    per_issue_concurrency: int = 1,
) -> Trigger | Rejection | None:
    """What the issue's trigger labels mean, or None if it has none.

    `active` is the issue's or PR's queued and dispatched runs (rows with
    `task` and `created_at`). Checked in order: labels the App added (removed
    quietly), a closed issue or PR, more than one trigger label (never a
    guessed precedence), the wrong kind of issue or PR, a task the enrolment
    doesn't enable, and a busy issue or PR. A label still there from before
    its own task's active run was queued is a leftover (the orchestrator
    stopped between queueing and removing it), removed quietly.
    """
    labels = [l for l in label_names(issue) if l in LABELS]
    if not labels:
        return None
    added = adders(github, repo, issue["number"])
    app = github.app_login()
    by_app = [l for l in labels if added.get(l, ("",))[0] == app]
    if by_app:
        return Rejection(by_app)
    mention = sorted({added[l][0] for l in labels if l in added})

    def reject(message: str) -> Rejection:
        return Rejection(labels, message, mention)

    what = "pull request" if "pull_request" in issue else "issue"
    named = ", ".join(f"`{l}`" for l in labels)
    if issue.get("state", "open") != "open":
        were = "was" if len(labels) == 1 else "were"
        return reject(f"This {what} is closed, so {named} {were} removed and nothing was started. "
                      "Reopen it first.")
    if len(labels) > 1:
        return reject(f"This {what} has more than one trigger label ({named}), so they were "
                      f"all removed and nothing was started. Add just one. {WHERE}")
    [label] = labels
    wrong = _wrong_kind(label, issue)
    if wrong is not None:
        return reject(f"{wrong} So `{label}` was removed and nothing was started.")
    missing = [t for t in NEEDS[label] if t not in enrolment.config.enabled_tasks()]
    if missing:
        listed = ", ".join(f"`{t}`" for t in missing)
        verb = "isn't" if len(missing) == 1 else "aren't"
        if missing == [LABELS[label]]:
            why = f"`{label}` isn't enabled for this repo"
        else:
            why = f"`{label}` can't start: it needs {listed}, which {verb} enabled for this repo"
        return reject(f"{why} (see `tasks` in `.factory/config.yml`), so the label was "
                      "removed and nothing was started.")
    who = added.get(label)
    if len(active) >= per_issue_concurrency:
        queued_after = [r for r in active if r["task"] == LABELS[label]
                        and (who is None or _time(who[1]) <= _time(r["created_at"]))]
        if queued_after:
            return Rejection(labels)
        return reject("A run is already in progress; re-add the label once it finishes.")
    return Trigger(label, LABELS[label], who[0] if who else None)


def _time(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _wrong_kind(label: str, issue: dict[str, Any]) -> str | None:
    """Why `label` doesn't belong on this kind of issue or PR, or None if it does."""
    names = label_names(issue)
    spec = next((n.removeprefix("spec:") for n in names if n.startswith("spec:")), None)
    if "pull_request" in issue:
        if label == "agent:review":
            return None
        goes = "a spec issue" if label == "agent:implement-spec" else "a ticket (an issue)"
        return (f"`{label}` goes on {goes}, not a pull request; "
                "to review a pull request, add `agent:review`.")
    if label == "agent:review":
        return "`agent:review` goes on a pull request, not an issue."
    if label == "agent:implement-spec" and spec is not None:
        return (f"This is a ticket of spec #{spec}: `agent:implement-spec` goes on the "
                f"spec issue, #{spec}.")
    if label == "agent:explore" and spec is not None:
        return (f"This is a ticket of spec #{spec}, and a spec's tickets skip explore: "
                f"they're covered by the spec's one review.")
    if label == "agent:explore" and "spec" in names:
        return ("This is a spec, which is explored as part of `agent:implement-spec`; "
                "add that to build it.")
    if label == "agent:implement" and "agent:built" in names:
        of = f" (spec #{spec})" if spec else ""
        return (f"This ticket is `agent:built`: its code has already landed on the spec's "
                f"integration branch, so fix it on the spec's pull request{of} instead.")
    return None


def label_names(issue: dict[str, Any]) -> list[str]:
    return [l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])]


def adders(github: GitHub, repo: str, number: int) -> dict[str, tuple[str, str]]:
    """Who last added each label to the issue, and when: label -> (login, ISO time)."""
    found: dict[str, tuple[str, str]] = {}
    for event in github.issue_events(repo, number):
        if event.get("event") == "labeled" and event.get("label") and event.get("actor"):
            found[event["label"]["name"]] = (event["actor"]["login"], event["created_at"])
    return found
