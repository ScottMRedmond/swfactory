"""The implement-ticket workflow end to end (orchestrator.md#implement-ticket):
where it can start, the `workflows` row's `pr` and `status`, an open factory PR
blocking a rebuild, and a PR closed without merging. GitHub's REST API is
in-memory (fake_github.py), its git side a real bare repo (git_repos.py), and
runs go through the fake runner."""

import json

from fake_github import APP_LOGIN, user_id

from factory import loop, orchestrator
from factory.runner import RunFiles
from test_explore_verdicts import explore_result
from test_implement_verdicts import (  # noqa: F401 (fixtures)
    REPO, commits, conn, enrolled, fake, finish_implement, github, remote, repo, runner, runs,
    settings,
)
from test_review_verdicts import finish_review, review_result


def workflow(conn):
    [row] = conn.execute("SELECT * FROM workflows").fetchall()
    return row


def labelled(repo, label, number=7):
    issue = repo.add_issue(number, "Search ignores accents", "Body", author="alice")
    issue.label(label, by="alice")
    return issue


def dispatched(conn, task):
    [row] = [r for r in runs(conn) if r["task"] == task and r["status"] == "dispatched"]
    return row


# --- end to end ------------------------------------------------------------------------


def test_explore_implement_review_end_to_end(conn, github, repo, remote, runner, settings):
    issue = labelled(repo, "agent:explore")
    orchestrator.run_once(conn, github, runner, settings)
    assert (workflow(conn)["status"], workflow(conn)["pr"]) == ("active", None)

    explore = dispatched(conn, "explore")
    runner.finish(explore["runner_run_id"], RunFiles(json.dumps(
        explore_result(explore["id"], verdict="easy-call")).encode()))
    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)
    implement = dispatched(conn, "implement")
    assert workflow(conn)["status"] == "active"

    finish_implement(conn, github, runner, settings, implement, commits(remote, implement))
    [pr] = repo.pulls.values()
    assert (workflow(conn)["status"], workflow(conn)["pr"]) == ("active", pr["number"])
    assert pr["draft"] is True and "agent:in-progress" in issue.labels

    orchestrator.run_once(conn, github, runner, settings)
    review = dispatched(conn, "review")
    finish_review(conn, github, runner, settings, review, review_result(review))

    assert [r["task"] for r in runs(conn)] == ["explore", "implement", "review"]
    assert all(r["workflow_id"] == workflow(conn)["id"] for r in runs(conn))
    assert workflow(conn)["status"] == "done"
    assert pr["draft"] is False and pr["requested_reviewers"] == ["alice"]
    assert issue.labels == [] and repo.issues[pr["number"]].labels == []
    assert f"Closes #{issue.number}" in pr["body"]


# --- starting from agent:implement -------------------------------------------------------


def test_agent_implement_starts_the_workflow_at_implement(conn, github, repo, runner, settings):
    issue = labelled(repo, "agent:implement")

    orchestrator.run_once(conn, github, runner, settings)

    [implement] = runs(conn)
    assert (implement["task"], implement["status"], implement["triggered_by"]) == (
        "implement", "dispatched", "alice")
    sent = runner.dispatched[-1][1]
    assert sent["branch"] == f"factory/7-{implement['id']}"
    assert sent["triggered_by"] == {"login": "alice", "id": user_id("alice")}
    assert "# Search ignores accents" in sent["ticket_markdown"]
    assert "agent:implement" not in issue.labels
    assert "agent:in-progress" in issue.labels
    assert (workflow(conn)["status"], workflow(conn)["subject"]) == ("active", "7")
    assert implement["workflow_id"] == workflow(conn)["id"]


def test_agent_implement_after_explore_stopped_reuses_the_workflow(
    conn, github, repo, runner, settings
):
    issue = labelled(repo, "agent:explore")
    orchestrator.run_once(conn, github, runner, settings)
    explore = dispatched(conn, "explore")
    runner.finish(explore["runner_run_id"], RunFiles(json.dumps(
        explore_result(explore["id"], verdict="needs-a-human",
                                              questions=["Which locale?"])).encode()))
    orchestrator.run_once(conn, github, runner, settings)
    assert workflow(conn)["status"] == "stopped" and "needs-a-human" in issue.labels
    issue.label("agent:implement", by="alice")

    orchestrator.run_once(conn, github, runner, settings)

    implement = dispatched(conn, "implement")
    assert implement["workflow_id"] == workflow(conn)["id"]
    assert workflow(conn)["status"] == "active"
    assert "needs-a-human" not in issue.labels and "agent:in-progress" in issue.labels


def built_and_reviewed(conn, github, repo, remote, runner, settings, label="agent:implement"):
    """The workflow, started with `label`, builds the PR and reviews it.
    Returns (issue, pr)."""
    issue = labelled(repo, label)
    orchestrator.run_once(conn, github, runner, settings)
    implement = dispatched(conn, "implement")
    finish_implement(conn, github, runner, settings, implement, commits(remote, implement))
    [pr] = repo.pulls.values()
    orchestrator.run_once(conn, github, runner, settings)
    review = dispatched(conn, "review")
    finish_review(conn, github, runner, settings, review, review_result(review))
    return issue, pr


def rebuild_notices(issue):
    return [c for c in issue.posts if "close the PR" in c["body"]]


def test_agent_implement_with_the_workflows_pr_open_queues_nothing_and_says_so(
    conn, github, repo, remote, runner, settings
):
    issue, pr = built_and_reviewed(conn, github, repo, remote, runner, settings)
    before = len(runs(conn))
    issue.label("agent:implement", by="alice")

    orchestrator.run_once(conn, github, runner, settings)

    assert len(runs(conn)) == before
    [notice] = rebuild_notices(issue)
    assert f"#{pr['number']}" in notice["body"] and "@alice" in notice["body"]
    assert "agent:implement" not in issue.labels
    assert "agent:in-progress" not in issue.labels
    assert workflow(conn)["status"] == "done"


def test_without_a_workflow_row_an_open_app_pr_closing_the_ticket_blocks_a_rebuild(
    conn, github, fake, repo, runner, settings
):
    fake.add_pull(REPO, 20, "Fold accents", "factory/7-3", author=APP_LOGIN, closes=[7])
    fake.add_pull(REPO, 21, "Someone else's go", "fix-accents", author="bob", closes=[7])
    issue = labelled(repo, "agent:implement")

    orchestrator.run_once(conn, github, runner, settings)

    assert runs(conn) == []
    [notice] = rebuild_notices(issue)
    assert "#20" in notice["body"]
    assert "agent:implement" not in issue.labels


def test_a_human_pr_closing_the_ticket_doesnt_block_implement(
    conn, github, fake, repo, runner, settings
):
    fake.add_pull(REPO, 21, "Someone else's go", "fix-accents", author="bob", closes=[7])
    fake.add_pull(REPO, 22, "Another ticket", "factory/8-4", author=APP_LOGIN, closes=[8])
    issue = labelled(repo, "agent:implement")

    orchestrator.run_once(conn, github, runner, settings)

    assert [r["task"] for r in runs(conn)] == ["implement"]
    assert rebuild_notices(issue) == []


def test_a_closed_workflow_pr_doesnt_block_a_rebuild(conn, github, repo, remote, runner, settings):
    issue, pr = built_and_reviewed(conn, github, repo, remote, runner, settings)
    pr["state"] = "closed"
    repo.issues[pr["number"]].state = "closed"
    orchestrator.run_once(conn, github, runner, settings)
    before = len(runs(conn))
    issue.label("agent:implement", by="alice")

    orchestrator.run_once(conn, github, runner, settings)

    [again] = runs(conn)[before:]
    assert again["task"] == "implement" and again["status"] == "dispatched"
    assert workflow(conn)["status"] == "active"
    assert "agent:in-progress" in issue.labels


# --- the PR is closed -------------------------------------------------------------------


def review_running(conn, github, repo, remote, runner, settings):
    """Explore, implement opens the PR, review is in flight. Returns (issue, pr, review row)."""
    issue = labelled(repo, "agent:implement")
    orchestrator.run_once(conn, github, runner, settings)
    implement = dispatched(conn, "implement")
    finish_implement(conn, github, runner, settings, implement, commits(remote, implement))
    [pr] = repo.pulls.values()
    orchestrator.run_once(conn, github, runner, settings)
    return issue, pr, dispatched(conn, "review")


def close(repo, pr, *, merged=False):
    pr["state"] = "closed"
    pr["merged"] = merged
    repo.issues[pr["number"]].state = "closed"


def closed_notices(issue):
    return [c for c in issue.posts if "closed without merging" in c["body"]]


def test_closing_the_pr_unmerged_cancels_its_review_and_the_workflow(
    conn, github, repo, remote, runner, settings
):
    issue, pr, review = review_running(conn, github, repo, remote, runner, settings)
    close(repo, pr)

    orchestrator.run_once(conn, github, runner, settings)

    assert review["runner_run_id"] in runner.cancelled
    assert loop.get_run(conn, review["id"])["outcome"] == "cancelled"
    assert workflow(conn)["status"] == "cancelled"
    [notice] = closed_notices(issue)
    assert f"PR #{pr['number']}" in notice["body"] and "`agent:implement`" in notice["body"]
    assert "agent:in-progress" not in issue.labels
    orchestrator.run_once(conn, github, runner, settings)
    assert len(closed_notices(issue)) == 1


def test_closing_a_reviewed_pr_unmerged_cancels_the_done_workflow(
    conn, github, repo, remote, runner, settings
):
    issue, pr = built_and_reviewed(conn, github, repo, remote, runner, settings)
    assert workflow(conn)["status"] == "done"
    close(repo, pr)

    orchestrator.run_once(conn, github, runner, settings)

    assert workflow(conn)["status"] == "cancelled"
    assert len(closed_notices(issue)) == 1


def test_a_merged_pr_leaves_the_workflow_done(conn, github, repo, remote, runner, settings):
    issue, pr = built_and_reviewed(conn, github, repo, remote, runner, settings)
    close(repo, pr, merged=True)
    issue.state = "closed"  # `Closes #7`

    orchestrator.run_once(conn, github, runner, settings)

    assert workflow(conn)["status"] == "done"
    assert closed_notices(issue) == []


def test_a_merged_pr_whose_ticket_stays_open_leaves_the_workflow_done(
    conn, github, repo, remote, runner, settings
):
    issue, pr = built_and_reviewed(conn, github, repo, remote, runner, settings)
    close(repo, pr, merged=True)

    orchestrator.run_once(conn, github, runner, settings)

    assert workflow(conn)["status"] == "done"
    assert closed_notices(issue) == []


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def test_an_incremental_poll_sees_the_pr_closed_since_the_last_poll(
    conn, github, fake, repo, remote, runner, settings
):
    issue, pr, review = review_running(conn, github, repo, remote, runner, settings)
    clock = Clock()
    poller = orchestrator.Poller(clock)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    close(repo, pr)
    repo.issues[pr["number"]].updated_at = fake.clock() + 30
    clock.now += 60
    sent = len(fake.requests)

    orchestrator.run_once(conn, github, runner, settings, poller=poller)

    assert any("since=" in r.url for r in fake.requests[sent:])
    assert loop.get_run(conn, review["id"])["outcome"] == "cancelled"
    assert workflow(conn)["status"] == "cancelled"
    assert len(closed_notices(issue)) == 1
