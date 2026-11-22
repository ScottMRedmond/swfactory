"""`factory doctor <repo>`: check an enrolment before a real ticket does.

It checks what the orchestrator can see as the App:

- the App is installed on the repo
- `.factory/config.yml` on the default branch's head is valid, and
  `.factory/Dockerfile` is there
- the caller workflow (`.github/workflows/factory.yml`) is on the default branch
- the factory's labels exist
- no ruleset blocks the factory's pushes to `factory/*` branches

then, unless the App, the enrolment or the caller workflow is missing,
dispatches a `smoke` run on the default branch's head and waits for it. The
smoke run builds the image and runs setup and the check, so it proves the
caller workflow, `run.yml`, the Dockerfile and the repo's commands end to end
without using Claude usage. It can't prove the Claude secret is there.

The App has no `administration` permission, so the target repo's Actions
settings (Actions enabled, read-only default workflow permissions, approval
for fork PRs) and classic branch protection can't be read; doctor lists them
to check by hand.

The smoke run is a row in `runs` like any other (task `smoke`), dispatched by
doctor itself whatever the limits. It isn't counted against the daily cap.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from factory import db, logs, loop
from factory.contract import CONTRACT_VERSION, ContractError, parse_enrolment
from factory.enrolment import CONFIG_PATH, LABELS
from factory.github import AppAuth, GitHub, GitHubError, urllib_transport
from factory.runner import Runner, RunnerUnavailable

log = logging.getLogger(__name__)

# Seams for tests.
TRANSPORT = urllib_transport
SLEEP = time.sleep

CALLER_PATH = ".github/workflows/factory.yml"
DOCKERFILE_PATH = ".factory/Dockerfile"
SMOKE_TIMEOUT = 20  # minutes, for the whole job
WAIT_SECONDS = 15

# A branch name the rulesets are asked about: rules match by pattern, so the
# branch needn't exist.
SAMPLE_BRANCH = "factory/doctor"
# Ruleset rules that stop the factory creating, pushing to or re-pushing a
# `factory/*` branch: its commits aren't signed, it pushes directly rather than
# through a PR, its integration branches take merge commits, and a retry
# replaces a branch's commits.
BLOCKING_RULES = {
    "creation": "restricts creating them",
    "update": "restricts pushing to them",
    "non_fast_forward": "blocks force pushes",
    "required_signatures": "requires signed commits",
    "pull_request": "requires a pull request",
    "required_status_checks": "requires status checks",
    "required_linear_history": "requires linear history",
}

BY_HAND = [
    "Actions is enabled",
    "default workflow permissions are read-only",
    "workflows from fork PRs need approval",
    "no classic branch protection rule covers factory/* (no required signed commits, pushes allowed)",
]


@dataclass(frozen=True)
class Check:
    passed: bool
    text: str


def run(
    conn: sqlite3.Connection,
    github: GitHub,
    runner: Runner,
    repo: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    """Check `repo`'s enrolment, then smoke-test it. 0 if all is well, else 1."""
    checks, head = _checks(github, repo, out)
    healthy = all(c.passed for c in checks)
    out("")
    out("Not readable by the App; check these by hand in the repo's settings:")
    for item in BY_HAND:
        out(f"  -  {item}")
    out("")
    if head is None:
        out("smoke run not dispatched: fix the failures above first")
        return 1
    return 0 if _smoke(conn, runner, repo, head, sleep, out) and healthy else 1


def _checks(github: GitHub, repo: str, out: Callable[[str], None]) -> tuple[list[Check], str | None]:
    """Every check, printed as it's made, and the head a smoke run can use
    (None if the App, the enrolment or the caller workflow is missing)."""
    checks: list[Check] = []

    def report(passed: bool, text: str) -> bool:
        checks.append(Check(passed, text))
        out(f"{'ok  ' if passed else 'FAIL'}  {text}")
        return passed

    installed = github.app_installed(repo)
    if not report(installed, "the App is installed" if installed else
                  f"the App isn't installed on {repo} (or not given access to it)"):
        return checks, None
    head = github.default_branch_head(repo)
    can_smoke = _enrolment(github, repo, head, report)
    caller = github.read_file(repo, CALLER_PATH, head)
    if caller is None:
        can_smoke = report(False, f"the caller workflow {CALLER_PATH} isn't on the default branch")
    elif "/.github/workflows/run.yml@" not in caller:
        can_smoke = report(False, f"the caller workflow {CALLER_PATH} doesn't call the factory's "
                                  "run.yml; copy runner/templates/factory.yml") and can_smoke
    else:
        report(True, f"the caller workflow {CALLER_PATH} is on the default branch")
    missing = [name for name, _, _ in LABELS if name not in github.labels(repo)]
    report(not missing, "the factory's labels exist" if not missing else
           f"labels missing: {', '.join(missing)} (`factory serve` creates them on its first poll)")
    _rules(github, repo, report)
    return checks, head if can_smoke else None


def _enrolment(github: GitHub, repo: str, head: str, report) -> bool:
    text = github.read_file(repo, CONFIG_PATH, head)
    if text is None:
        return report(False, f"{CONFIG_PATH} isn't on the default branch")
    try:
        parse_enrolment(text)
    except ContractError as e:
        lines = [line.strip() for line in str(e).splitlines()]
        why = "; ".join(line for line in lines if line and not line.startswith("For further"))
        return report(False, f"{CONFIG_PATH} is invalid: {why}")
    report(True, f"{CONFIG_PATH} is valid (at {head[:7]})")
    if github.read_file(repo, DOCKERFILE_PATH, head) is None:
        return report(False, f"{DOCKERFILE_PATH} isn't on the default branch")
    return report(True, f"{DOCKERFILE_PATH} is there (the smoke run builds it)")


def _rules(github: GitHub, repo: str, report) -> None:
    try:
        rules = github.branch_rules(repo, SAMPLE_BRANCH)
    except GitHubError as e:
        if e.status == 403 and "Upgrade to GitHub Pro" in str(e):
            # A private repo on a free plan: rulesets aren't available, so none apply.
            report(True, "no ruleset blocks the factory's pushes to factory/* branches "
                         "(rulesets aren't available to this private repo on its plan)")
        else:
            report(False, f"can't read the rulesets for factory/* branches: {e}")
        return
    blocking = [r for r in rules if r.get("type") in BLOCKING_RULES]
    if not blocking:
        report(True, "no ruleset blocks the factory's pushes to factory/* branches")
    for rule in blocking:
        source = rule.get("ruleset_source") or "a ruleset"
        report(False, f"a ruleset ({source}, id {rule.get('ruleset_id')}) on factory/* branches "
                      f"{BLOCKING_RULES[rule['type']]} ({rule['type']})")


def _smoke(conn, runner: Runner, repo: str, head: str, sleep, out) -> bool:
    """Dispatch a smoke run on `head` and wait for it. True if it succeeded."""
    dispatch = {
        "contract_version": CONTRACT_VERSION,
        "repo": repo,
        "task": "smoke",
        "base_sha": head,
        "enrolment_sha": head,
        "timeout_minutes": SMOKE_TIMEOUT,
    }
    run_id = loop.enqueue(conn, repo=repo, task="smoke", dispatch=dispatch)
    try:
        loop.dispatch_run(conn, runner, loop.get_run(conn, run_id))
    except RunnerUnavailable as e:
        loop.cancel(conn, runner, loop.get_run(conn, run_id))
        out(f"smoke run {run_id} couldn't be dispatched: {e}")
        return False
    row = loop.get_run(conn, run_id)
    out(f"smoke run {run_id} dispatched: {row['log_url']}")
    out("waiting for it to finish (Ctrl-C stops waiting; the orchestrator still collects it)...")
    while row["status"] != "done":
        sleep(WAIT_SECONDS)
        loop.collect(conn, runner, only=run_id)
        row = loop.get_run(conn, run_id)
    out(f"smoke run {run_id} {row['outcome']}: {row['log_url']}")
    result = json.loads(row["result"]) if row["result"] else {}
    if result.get("failure"):
        out(f"  failure: {result['failure']}")
    tail = (result.get("check") or {}).get("output_tail")
    if tail and row["outcome"] != "succeeded":
        out("  check output (tail):")
        for line in tail.rstrip("\n").splitlines():
            out(f"    {line}")
    if row["outcome"] in ("invalid", "failed") and not row["result"]:
        out("  it left no valid result.json; see the runner log (`factory logs --run "
            f"{run_id}` for why)")
    return row["outcome"] == "succeeded"


# --- the command ---


def add_parser(commands: argparse._SubParsersAction) -> None:
    doctor = commands.add_parser(
        "doctor", help="check a repo's enrolment, then prove it with a smoke run"
    )
    doctor.add_argument("repo", help="owner/name")


def main(args: argparse.Namespace) -> int:
    app_id = os.environ.get("FACTORY_APP_ID")
    key_file = os.environ.get("FACTORY_APP_KEY_FILE")
    if not app_id or not key_file:
        print("factory doctor: set FACTORY_APP_ID and FACTORY_APP_KEY_FILE", file=sys.stderr)
        return 2
    try:
        private_key = Path(key_file).read_text()
    except OSError as e:
        print(f"factory doctor: {e}", file=sys.stderr)
        return 2
    from factory.actions_runner import ActionsRunner

    handler = logs.setup(args.logs)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    auth = AppAuth(app_id, private_key, TRANSPORT)
    try:
        with closing(db.connect(args.db)) as conn:
            return run(conn, GitHub(auth, TRANSPORT), ActionsRunner(TRANSPORT, auth), args.repo,
                       sleep=SLEEP)
    except (GitHubError, OSError) as e:
        print(f"factory doctor: GitHub failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped waiting; the orchestrator collects the smoke run", file=sys.stderr)
        return 1
    finally:
        logging.getLogger("factory").removeHandler(handler)
        handler.close()
