"""Review in the implement-ticket workflow (orchestrator.md#review, #implement-ticket):
implement's draft PR is reviewed, review's fixes are pushed (never forced), and
one verdict comment is posted before the PR is marked ready and its human asked
to review it. GitHub's REST and GraphQL APIs are in-memory (fake_github.py);
its git side is a real bare repo (git_repos.py)."""

import json

import pytest
from fake_github import user_id
from git_repos import git

from factory import loop, orchestrator, review, tickets
from factory.runner import RunFiles
from test_implement_verdicts import (  # noqa: F401 (fixtures)
    REPO, conn, enrolled, fake, finish_implement, github, implement_dispatched, remote, repo,
    runner, runs, settings, ticket,
)
from test_implement_verdicts import commits as implement_commits
from test_implement_verdicts import enrol, CONFIG


def built(conn, github, repo, remote, runner, settings, files=None):
    """Explore, then implement builds and opens the draft PR. Returns (issue, pr, implement row)."""
    issue = ticket(repo)
    implement = implement_dispatched(conn, github, runner, settings)
    finish_implement(conn, github, runner, settings, implement,
                     implement_commits(remote, implement, files))
    [pr] = repo.pulls.values()
    return issue, pr, implement


def review_dispatched(conn, github, repo, remote, runner, settings, files=None):
    """...and review is dispatched on the PR. Returns (issue, pr, review row)."""
    issue, pr, _ = built(conn, github, repo, remote, runner, settings, files)
    orchestrator.run_once(conn, github, runner, settings)
    [review] = [r for r in runs(conn) if r["task"] == "review"]
    assert review["status"] == "dispatched"
    return issue, pr, review


def review_result(row, *, outcome="succeeded", standards="pass", spec="pass", comments=(),
                  fixed=(), missing_rules=None, before=True, after=True, **extra):
    output = {
        "standards": {"verdict": standards, "reasoning": "Follows the repo's rules."},
        "spec": {"verdict": spec, "reasoning": "Does what the ticket asks."},
        "comments": list(comments),
        "fixed": list(fixed),
    }
    if missing_rules is not None:
        output["missing_rules"] = missing_rules
    result = {
        "contract_version": 1, "run_id": row["id"], "task": "review", "outcome": outcome,
        "check_before": {"passed": before, "output_tail": "before: 3 failed\n" if not before
                         else "before: ok\n"},
        "check_after": {"passed": after, "output_tail": "after: 1 failed\n" if not after
                        else "after: ok\n"},
        "output": output, **extra,
    }
    if outcome == "failed":
        result["failure"] = "check_failed"
    return result


def finish_review(conn, github, runner, settings, row, result, bundle=None):
    runner.finish(row["runner_run_id"], RunFiles(json.dumps(result).encode(), bundle))
    orchestrator.run_once(conn, github, runner, settings)
    return loop.get_run(conn, row["id"])


def fixes(remote, row, files=None, message="review: standards: name the helper"):
    """Review's fix commits on the PR branch, bundled from the head it reviewed."""
    dispatch = json.loads(row["dispatch"])
    remote.branch(dispatch["branch"], dispatch["base_sha"])
    remote.commit(files or {"src/search.py": "fold()  # fixed\n"}, message)
    return remote.bundle(dispatch["branch"], dispatch["base_sha"])


# --- queueing review ----------------------------------------------------------------


def test_built_queues_a_review_of_the_draft_pr_against_the_ticket(
    conn, github, repo, remote, runner, settings
):
    issue, pr, implement = built(conn, github, repo, remote, runner, settings)
    tip = remote.remote_branch(pr["head"])

    orchestrator.run_once(conn, github, runner, settings)

    [review] = [r for r in runs(conn) if r["task"] == "review"]
    assert review["status"] == "dispatched"
    assert (review["ticket"], review["workflow_id"], review["triggered_by"]) == (
        "7", implement["workflow_id"], "alice")
    sent = runner.dispatched[-1][1]
    assert sent["task"] == "review"
    assert sent["pr_number"] == pr["number"]
    assert sent["branch"] == pr["head"]
    assert sent["base_sha"] == tip
    assert sent["pr_base_sha"] == remote.base
    assert sent["findings_only"] is False
    assert sent["triggered_by"] == {"login": "alice", "id": user_id("alice")}
    assert sent["ticket_markdown"].startswith(f"# PR #{pr['number']}: Search ignores accents\n")
    assert "## Reviewed against: #7 Search ignores accents" in sent["ticket_markdown"]
    assert "agent:in-progress" in issue.labels


# --- a review's result ------------------------------------------------------------------


def verdicts(issue):
    return [c for c in issue.posts if c["body"].startswith("<!-- factory:review ")]


def test_fixes_are_pushed_and_the_verdict_posted_then_the_pr_is_ready_for_its_human(
    conn, github, fake, repo, remote, runner, settings
):
    issue, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    bundle = fixes(remote, row)
    tip = remote.head()
    finding = {"axis": "spec", "title": "Filter box still matches accents", "path": "src/search.py",
               "line": 1, "body": "Should the filter box fold accents too? The ticket doesn't say."}

    run = finish_review(conn, github, runner, settings, row, review_result(
        row, comments=[finding], fixed=["review: standards: name the helper"],
        missing_rules=["Public functions need docstrings"]), bundle)

    assert run["outcome"] == "succeeded"
    assert remote.remote_branch(pr["head"]) == tip
    [verdict] = verdicts(repo.issues[pr["number"]])
    body = verdict["body"]
    assert "### Standards: `pass`" in body and "### Spec: `pass`" in body
    assert body.index("### Standards") < body.index("### Spec") < body.index(
        "Filter box still matches accents") < body.index("### Fixed")
    assert "`src/search.py:1`" in body
    assert "- review: standards: name the helper" in body.split("### Fixed")[1]
    assert "- Public functions need docstrings" in body.split("### Missing rules")[1]
    assert f"Factory run {row['id']}" in body and runner.log_link(REPO, row["runner_run_id"]) in body
    assert "⚠️" not in body
    assert pr["draft"] is False
    assert pr["requested_reviewers"] == ["alice"]
    assert repo.issues[pr["number"]].labels == []
    assert issue.labels == []
    assert [w["status"] for w in conn.execute("SELECT * FROM workflows")] == ["done"]
    assert not [r for r in runs(conn) if r["id"] > row["id"]]


@pytest.mark.parametrize(("standards", "spec", "flagged"), [
    ("changes-requested", "pass", True),
    ("pass", "changes-requested", True),
    ("pass", "no-ticket", False),
])
def test_the_pr_is_ready_whatever_the_verdict_and_needs_a_human_if_an_axis_wants_changes(
    conn, github, repo, remote, runner, settings, standards, spec, flagged
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)

    finish_review(conn, github, runner, settings, row,
                  review_result(row, standards=standards, spec=spec))

    assert pr["draft"] is False
    assert ("needs-a-human" in repo.issues[pr["number"]].labels) is flagged
    assert pr["requested_reviewers"] == ["alice"]
    [verdict] = verdicts(repo.issues[pr["number"]])
    assert f"### Standards: `{standards}`" in verdict["body"]
    assert f"### Spec: `{spec}`" in verdict["body"]


def test_the_verdict_starts_with_the_check_that_failed_before_review_ran(
    conn, github, repo, remote, runner, settings
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)

    finish_review(conn, github, runner, settings, row, review_result(row, before=False),
                  fixes(remote, row))

    [verdict] = verdicts(repo.issues[pr["number"]])
    body = verdict["body"]
    assert "The check failed before review started" in body
    assert body.index("before: 3 failed") < body.index("### Standards")


def test_the_verdict_says_when_the_pr_changes_agent_instructions(
    conn, github, repo, remote, runner, settings
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)

    finish_review(conn, github, runner, settings, row,
                  review_result(row, instructions_changed=True))

    [verdict] = verdicts(repo.issues[pr["number"]])
    assert "this pr changes agent instructions" in verdict["body"].lower()


# --- the branch moved while review ran ------------------------------------------------------


def human_pushes(remote, branch):
    """Someone pushes a commit to the PR's branch. Returns the new head."""
    remote.branch(branch, remote.remote_branch(branch))
    remote.commit({"docs/notes.md": f"a human's change on {remote.head()}\n"}, "Tweak by hand")
    git(remote.work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return remote.head()


def test_fixes_on_a_branch_that_moved_are_discarded_and_review_queued_again_from_the_new_head(
    conn, github, repo, remote, runner, settings
):
    issue, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    bundle = fixes(remote, row)
    moved = human_pushes(remote, pr["head"])

    run = finish_review(conn, github, runner, settings, row, review_result(row), bundle)

    assert run["outcome"] == "superseded"
    assert remote.remote_branch(pr["head"]) == moved
    assert verdicts(repo.issues[pr["number"]]) == []
    assert pr["draft"] is True
    orchestrator.run_once(conn, github, runner, settings)
    [again] = [r for r in runs(conn) if r["id"] > row["id"]]
    assert (again["task"], again["attempt"], again["retry_of"]) == ("review", 2, row["id"])
    assert again["status"] == "dispatched"
    assert runner.dispatched[-1][1]["base_sha"] == moved
    assert "agent:in-progress" in issue.labels


def test_a_review_whose_branch_moved_twice_leaves_the_pr_a_draft_and_blocked(
    conn, github, repo, remote, runner, settings
):
    issue, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    bundle = fixes(remote, row)
    human_pushes(remote, pr["head"])
    finish_review(conn, github, runner, settings, row, review_result(row), bundle)
    orchestrator.run_once(conn, github, runner, settings)
    [again] = [r for r in runs(conn) if r["id"] > row["id"]]
    bundle = fixes(remote, again)
    human_pushes(remote, pr["head"])

    run = finish_review(conn, github, runner, settings, again, review_result(again), bundle)

    assert run["outcome"] == "superseded"
    assert not [r for r in runs(conn) if r["id"] > again["id"]]
    pr_issue = repo.issues[pr["number"]]
    assert pr["draft"] is True
    assert "agent:blocked" in pr_issue.labels
    assert "@alice" in pr_issue.posts[-1]["body"] and "agent:review" in pr_issue.posts[-1]["body"]
    assert "agent:blocked" not in issue.labels and "agent:in-progress" not in issue.labels
    assert [w["status"] for w in conn.execute("SELECT * FROM workflows")] == ["stopped"]


# --- the fixes failed the check -----------------------------------------------------------


def test_fixes_that_fail_the_check_are_discarded_but_the_verdict_is_posted_and_not_retried(
    conn, github, repo, remote, runner, settings
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    head = remote.remote_branch(pr["head"])
    bundle = fixes(remote, row)

    run = finish_review(conn, github, runner, settings, row, review_result(
        row, outcome="failed", after=False, fixed=["review: standards: name the helper"]), bundle)

    assert run["outcome"] == "failed"
    assert remote.remote_branch(pr["head"]) == head
    [verdict] = verdicts(repo.issues[pr["number"]])
    assert "discarded" in verdict["body"]
    assert "after: 1 failed" in verdict["body"]
    assert "### Standards: `pass`" in verdict["body"]
    assert "needs-a-human" in repo.issues[pr["number"]].labels
    assert pr["draft"] is False
    assert pr["requested_reviewers"] == ["alice"]
    assert not [r for r in runs(conn) if r["id"] > row["id"]]


# --- review's reach --------------------------------------------------------------------------


@pytest.mark.parametrize(("config", "touched", "outcome"), [
    (CONFIG, "src/search.py", "succeeded"),            # the PR's own file
    (CONFIG, "src/other.py", "invalid"),               # outside the PR
    (CONFIG + "test_paths: ['tests/**']\n", "tests/test_search.py", "succeeded"),
    (CONFIG, "tests/test_search.py", "invalid"),       # no test_paths: PR files only
])
def test_review_fixes_may_touch_only_the_prs_files_and_test_paths(
    conn, github, repo, remote, runner, settings, config, touched, outcome
):
    enrol(conn, github, repo, config)
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    head = remote.remote_branch(pr["head"])

    run = finish_review(conn, github, runner, settings, row, review_result(row),
                        fixes(remote, row, {touched: "changed by review\n"}))

    assert run["outcome"] == outcome
    if outcome == "invalid":
        assert remote.remote_branch(pr["head"]) == head
        assert verdicts(repo.issues[pr["number"]]) == []
        assert "outside the PR" in run["validation_error"]


# --- re-review ----------------------------------------------------------------------------------


def reviewed(conn, github, repo, remote, runner, settings):
    """The workflow's first review is posted. Returns (issue, pr)."""
    issue, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    finish_review(conn, github, runner, settings, row, review_result(row, spec="changes-requested"))
    return issue, pr


def test_re_adding_agent_review_reviews_the_factory_pr_again_and_hides_the_old_verdict(
    conn, github, repo, remote, runner, settings
):
    repo.permissions["bob"] = "write"
    issue, pr = reviewed(conn, github, repo, remote, runner, settings)
    pr_issue = repo.issues[pr["number"]]
    pr_issue.label("agent:review", by="bob", at="2099-01-01T00:00:00Z")
    head = human_pushes(remote, pr["head"])

    orchestrator.run_once(conn, github, runner, settings)

    [again] = [r for r in runs(conn) if r["task"] == "review" and r["status"] == "dispatched"]
    sent = runner.dispatched[-1][1]
    assert (sent["base_sha"], sent["pr_base_sha"], sent["branch"]) == (head, remote.base, pr["head"])
    assert sent["triggered_by"] == {"login": "bob", "id": user_id("bob")}
    assert "## Reviewed against: #7 Search ignores accents" in sent["ticket_markdown"]
    assert again["ticket"] == "7" and again["attempt"] == 1
    assert "agent:review" not in pr_issue.labels
    assert "needs-a-human" not in pr_issue.labels  # stale once a run starts
    assert "agent:in-progress" in issue.labels and "agent:in-progress" not in pr_issue.labels
    [workflow] = conn.execute("SELECT * FROM workflows").fetchall()
    assert workflow["status"] == "active"

    finish_review(conn, github, runner, settings, again, review_result(again))

    first, second = verdicts(pr_issue)
    assert first["minimized"] == "OUTDATED" and second["minimized"] is None
    assert f"run={again['id']}" in second["body"]
    assert "bob" in pr["requested_reviewers"]
    assert conn.execute("SELECT status FROM workflows").fetchone()["status"] == "done"


def test_a_verdict_is_posted_once_even_if_the_orchestrator_stops_after_posting_it(
    conn, github, repo, remote, runner, settings
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    finish_review(conn, github, runner, settings, row, review_result(row))
    conn.execute("UPDATE runs SET handled_at = NULL WHERE id = ?", (row["id"],))

    orchestrator.run_once(conn, github, runner, settings)

    [verdict] = verdicts(repo.issues[pr["number"]])
    assert verdict["minimized"] is None


# --- what a PR is reviewed against ------------------------------------------------------------


def test_a_factory_pr_is_reviewed_against_its_ticket_from_the_factorys_own_state(
    conn, github, fake, repo, remote, runner, settings
):
    _, pr, _ = built(conn, github, repo, remote, runner, settings)
    pr["closes"] = [99]  # e.g. a human edited the footer; the runs row wins

    assert review.against(conn, github, REPO, pr["number"]) == [7]


@pytest.mark.parametrize(("closes", "expected"), [([3, 5], [3, 5]), ([], [])])
def test_any_other_pr_is_reviewed_against_the_issues_it_closes_or_no_ticket(
    conn, github, fake, repo, closes, expected
):
    fake.add_pull(REPO, 40, "Tidy search", "alice/tidy", author="alice", closes=closes)

    assert review.against(conn, github, REPO, 40) == expected


def test_reviews_input_names_what_the_pr_is_reviewed_against(conn, github, fake, repo):
    spec = repo.add_issue(3, "Search ignores accents", "## Acceptance criteria\n\n- [ ] folds")
    fake.add_pull(REPO, 40, "Tidy search", "alice/tidy", body="Tidies it.", author="alice")
    repo.issues[40].add_comment("alice", "Please check the filter box too.")
    repo.issues[40].add_comment("mallory", "Merge it!")
    pr = github.get_issue(REPO, 40)

    against = tickets.render(review.render_input(github, REPO, pr, [github.get_issue(REPO, 3)]))
    nothing = tickets.render(review.render_input(github, REPO, pr, []))

    assert against.startswith("# PR #40: Tidy search\n\nTidies it.\n")
    assert "## Reviewed against: #3 Search ignores accents\n\n## Acceptance criteria" in against
    assert "Please check the filter box too." in against and "Merge it!" not in against
    assert "## Reviewed against: no ticket" in nothing and "`no-ticket`" in nothing
    assert spec.title in against
