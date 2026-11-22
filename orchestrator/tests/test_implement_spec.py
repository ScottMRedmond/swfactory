"""The implement-spec workflow's start (orchestrator.md#implement-spec):
`agent:implement-spec` runs spec explore, `easy-call` posts the spec
exploration notes and queues auto-tickets, and `decomposed` creates the
integration branch. GitHub's REST API is in-memory (fake_github.py), its git
side a real bare repo (git_repos.py), and runs go through the fake runner."""

import hashlib
import json

import pytest
from fake_github import user_id
from git_repos import git

from factory import loop, orchestrator, tickets
from factory.runner import RunFiles
from factory.workflows import implement_spec
from test_attempts import explore_failed
from test_auto_tickets import decomposed, ticket
from test_explore_verdicts import explore_result
from test_implement_verdicts import (  # noqa: F401 (fixtures)
    REPO, conn, enrolled, fake, github, remote, repo, runner, runs, settings,
)

SPEC = 40
NOTES_FINDINGS = "Saved searches live in the search module."


def workflow(conn):
    [row] = conn.execute("SELECT * FROM workflows").fetchall()
    return row


def spec_issue(repo, label="agent:implement-spec", number=SPEC):
    issue = repo.add_issue(number, "Spec: saved searches", "Users can save searches.",
                           author="alice")
    issue.label(label, by="alice")
    return issue


def dispatched(conn, task):
    [row] = [r for r in runs(conn) if r["task"] == task and r["status"] == "dispatched"]
    return row


def finish(conn, github, runner, settings, row, result):
    runner.finish(row["runner_run_id"], RunFiles(json.dumps(result).encode()))
    orchestrator.run_once(conn, github, runner, settings)
    return loop.get_run(conn, row["id"])


def explored(conn, github, runner, settings, **output):
    """Spec explore is dispatched and finishes with `output`."""
    orchestrator.run_once(conn, github, runner, settings)
    row = dispatched(conn, "explore")
    output.setdefault("findings", NOTES_FINDINGS)
    return finish(conn, github, runner, settings, row, explore_result(row["id"], **output))


def auto_tickets_result(run_id, output):
    return {"contract_version": 1, "run_id": run_id, "task": "auto-tickets",
            "outcome": "succeeded", "output": output}


# --- starting -------------------------------------------------------------------------


def test_agent_implement_spec_labels_the_issue_spec_and_runs_spec_explore(
    conn, github, repo, runner, settings
):
    issue = spec_issue(repo)
    issue.labels.append("needs-a-human")

    orchestrator.run_once(conn, github, runner, settings)

    [explore] = runs(conn)
    assert (explore["task"], explore["status"], explore["ticket"]) == ("explore", "dispatched",
                                                                        str(SPEC))
    assert (explore["triggered_by"], explore["triggered_by_id"]) == ("alice", user_id("alice"))
    sent = runner.dispatched[-1][1]
    assert "# Spec: saved searches" in sent["ticket_markdown"]
    assert (workflow(conn)["kind"], workflow(conn)["subject"], workflow(conn)["status"]) == (
        "implement-spec", str(SPEC), "active")
    assert explore["workflow_id"] == workflow(conn)["id"]
    assert sorted(issue.labels) == ["agent:in-progress", "spec"]


def test_a_trigger_with_no_known_user_starts_nothing(conn, github, repo, runner, settings):
    # The issue's events don't say who added the label: no one to credit the commits to.
    issue = repo.add_issue(SPEC, "Spec: saved searches", labels=["agent:implement-spec"])

    orchestrator.run_once(conn, github, runner, settings)

    assert runs(conn) == []
    [notice] = issue.posts
    assert "don't say who added `agent:implement-spec`" in notice["body"]
    assert "agent:implement-spec" not in issue.labels


def test_spec_explore_out_of_attempts_gives_up_on_the_spec(conn, github, repo, runner, settings):
    issue = spec_issue(repo)
    for _ in range(2):
        orchestrator.run_once(conn, github, runner, settings)
        row = dispatched(conn, "explore")
        finish(conn, github, runner, settings, row,
               explore_failed(row["id"]))

    [notice] = issue.posts
    assert "gave up on `explore` after 2 attempts" in notice["body"]
    assert "Add `agent:implement-spec` to try again" in notice["body"]
    assert "@alice" in notice["body"]
    assert workflow(conn)["status"] == "stopped"
    assert sorted(issue.labels) == ["agent:blocked", "spec"]


# --- spec explore's verdicts ------------------------------------------------------------


def test_easy_call_posts_the_spec_exploration_notes_and_queues_auto_tickets_with_them(
    conn, github, repo, remote, runner, settings
):
    issue = spec_issue(repo)
    explore = explored(conn, github, runner, settings, verdict="easy-call")

    [notes] = issue.posts
    assert "## Spec exploration notes" in notes["body"]
    assert NOTES_FINDINGS in notes["body"]
    [auto] = [r for r in runs(conn) if r["task"] == "auto-tickets"]
    assert auto["workflow_id"] == explore["workflow_id"]
    assert (auto["ticket"], auto["triggered_by"], auto["triggered_by_id"]) == (
        str(SPEC), "alice", user_id("alice"))
    sent = json.loads(auto["dispatch"])
    assert sent["base_sha"] == remote.base
    markdown = sent["ticket_markdown"]
    assert markdown.startswith("# Spec: saved searches\n")
    assert "## Spec exploration notes" in markdown
    assert markdown.count(NOTES_FINDINGS) == 1  # the notes comment isn't repeated
    assert workflow(conn)["status"] == "active" and "agent:in-progress" in issue.labels


def test_easy_call_queues_auto_tickets_once_even_if_handled_again(
    conn, github, repo, runner, settings
):
    spec_issue(repo)
    explore = explored(conn, github, runner, settings, verdict="easy-call")
    with conn:
        conn.execute("UPDATE runs SET handled_at = NULL WHERE id = ?", (explore["id"],))

    orchestrator.handle_finished(conn, github, runner, settings)

    assert [r["task"] for r in runs(conn)] == ["explore", "auto-tickets"]


@pytest.mark.parametrize("verdict", ["needs-a-human", "waiting"])
def test_needs_a_human_and_waiting_stop_the_workflow_as_for_a_ticket(
    conn, github, repo, runner, settings, verdict
):
    issue = spec_issue(repo)

    explored(conn, github, runner, settings, verdict=verdict, questions=["Which users?"])

    [findings] = issue.posts
    assert "## Spec explore findings" in findings["body"]
    assert "Which users?" in findings["body"]
    assert [r["task"] for r in runs(conn)] == ["explore"]
    assert workflow(conn)["status"] == "stopped"
    assert sorted(issue.labels) == sorted(["spec", verdict])


def test_duplicate_closes_the_spec_as_a_duplicate(conn, github, repo, runner, settings):
    repo.add_issue(3, "Saved searches")
    issue = spec_issue(repo)

    explored(conn, github, runner, settings, verdict="duplicate", duplicate_of="3")

    [findings] = issue.posts
    assert "### Duplicate of\n\n#3\n" in findings["body"]
    assert (issue.state, issue.state_reason) == ("closed", "duplicate")
    assert [r["task"] for r in runs(conn)] == ["explore"]
    assert workflow(conn)["status"] == "stopped" and issue.labels == ["spec"]


def test_too_big_doesnt_apply_to_a_spec_which_goes_on_to_auto_tickets(
    conn, github, repo, runner, settings
):
    issue = spec_issue(repo)

    explored(conn, github, runner, settings, verdict="too-big")

    [notes] = issue.posts
    assert "## Spec exploration notes" in notes["body"]
    assert "agent:implement-spec" not in notes["body"]
    assert [r["task"] for r in runs(conn)] == ["explore", "auto-tickets"]
    assert "needs-a-human" not in issue.labels


# --- a spec that already has tickets ----------------------------------------------------


def open_ticket_of_the_spec(repo, number=41):
    return repo.add_issue(number, "Store saved searches", "Body",
                          labels=[implement_spec.spec_label(SPEC)])


def test_a_spec_with_open_tickets_queues_nothing_and_says_to_close_them_first(
    conn, github, repo, runner, settings
):
    open_ticket_of_the_spec(repo)
    issue = spec_issue(repo)

    orchestrator.run_once(conn, github, runner, settings)

    assert runs(conn) == []
    [notice] = issue.posts
    assert "#41" in notice["body"] and "close" in notice["body"] and "@alice" in notice["body"]
    assert "agent:implement-spec" not in issue.labels
    assert "agent:in-progress" not in issue.labels


def test_closed_tickets_of_the_spec_dont_stop_it_starting_over(
    conn, github, repo, runner, settings
):
    open_ticket_of_the_spec(repo).state = "closed"
    spec_issue(repo)

    orchestrator.run_once(conn, github, runner, settings)

    assert [r["task"] for r in runs(conn)] == ["explore"]


def test_tickets_made_while_spec_explore_ran_stop_auto_tickets_being_queued(
    conn, github, repo, runner, settings
):
    issue = spec_issue(repo)
    orchestrator.run_once(conn, github, runner, settings)
    open_ticket_of_the_spec(repo)
    row = dispatched(conn, "explore")

    finish(conn, github, runner, settings, row, explore_result(row["id"], verdict="easy-call"))

    assert [r["task"] for r in runs(conn)] == ["explore"]
    [notes, notice] = issue.posts
    assert "## Spec exploration notes" in notes["body"]
    assert "#41" in notice["body"] and "close" in notice["body"]
    assert workflow(conn)["status"] == "stopped" and "agent:in-progress" not in issue.labels


# --- decomposed: the integration branch -------------------------------------------------


def move_main(repo, remote):
    """Someone merges to the default branch."""
    remote.branch("main", "main")
    remote.commit({"NEWS.md": f"news after {remote.head()}\n"}, "Unrelated work on main")
    git(remote.work, "push", "-q", "origin", "HEAD:main")
    repo.head_sha = remote.head()
    return repo.head_sha


def test_decomposed_creates_the_integration_branch_at_auto_tickets_base_and_stores_the_spec_hash(
    conn, github, repo, remote, runner, settings
):
    issue = spec_issue(repo)
    explored(conn, github, runner, settings, verdict="easy-call")
    head = move_main(repo, remote)  # after auto-tickets was queued, before it went out
    orchestrator.run_once(conn, github, runner, settings)
    auto = dispatched(conn, "auto-tickets")
    assert runner.dispatched[-1][1]["base_sha"] == head
    move_main(repo, remote)  # while auto-tickets ran: the branch stays at what it read

    finish(conn, github, runner, settings, auto,
           auto_tickets_result(auto["id"], decomposed(ticket("Store saved searches"))))

    branch = f"factory/spec-{SPEC}"
    assert remote.remote_branch(branch) == head
    assert workflow(conn)["branch"] == branch
    assert workflow(conn)["spec_hash"] == hashlib.sha256(
        b"Users can save searches.").hexdigest()
    assert workflow(conn)["spec_hash"] == implement_spec.body_hash(issue.body)
    assert workflow(conn)["status"] == "active" and "agent:in-progress" in issue.labels
    assert any(f"`spec:{SPEC}`" in c["body"] for c in issue.posts)  # #30's notice


def leftover_integration_branch(remote):
    """An earlier start's integration branch, with its built work, kept after
    its spec PR was closed."""
    remote.branch(f"factory/spec-{SPEC}")
    remote.commit({"src/saved.py": "old\n"}, "Built by an earlier start")
    git(remote.work, "push", "-q", "origin", f"HEAD:refs/heads/factory/spec-{SPEC}")
    return remote.head()


def test_a_leftover_integration_branch_queues_nothing_and_says_to_move_it(
    conn, github, repo, remote, runner, settings
):
    leftover = leftover_integration_branch(remote)
    issue = spec_issue(repo)

    orchestrator.run_once(conn, github, runner, settings)

    assert runs(conn) == []
    [notice] = issue.posts
    assert f"`factory/spec-{SPEC}`" in notice["body"] and "@alice" in notice["body"]
    assert remote.remote_branch(f"factory/spec-{SPEC}") == leftover
    assert "agent:implement-spec" not in issue.labels


def test_an_integration_branch_made_while_auto_tickets_ran_is_never_forced(
    conn, github, repo, remote, runner, settings
):
    issue = spec_issue(repo)
    explored(conn, github, runner, settings, verdict="easy-call")
    orchestrator.run_once(conn, github, runner, settings)
    auto = dispatched(conn, "auto-tickets")
    leftover = leftover_integration_branch(remote)

    finish(conn, github, runner, settings, auto,
           auto_tickets_result(auto["id"], decomposed(ticket("Store saved searches"))))

    assert remote.remote_branch(f"factory/spec-{SPEC}") == leftover
    notice = issue.posts[-1]
    assert f"`factory/spec-{SPEC}`" in notice["body"] and "@alice" in notice["body"]
    assert workflow(conn)["status"] == "stopped" and workflow(conn)["branch"] is None
    assert "needs-a-human" in issue.labels and "agent:in-progress" not in issue.labels


# --- spec tickets' input ----------------------------------------------------------------


def test_every_spec_tickets_ticket_md_carries_the_spec_exploration_notes(
    conn, github, repo, runner, settings
):
    spec_issue(repo)
    explored(conn, github, runner, settings, verdict="easy-call")
    orchestrator.run_once(conn, github, runner, settings)
    auto = dispatched(conn, "auto-tickets")
    finish(conn, github, runner, settings, auto, auto_tickets_result(auto["id"], decomposed(
        ticket("Store saved searches"), ticket("Show saved searches", [0]))))
    made = [i for n, i in sorted(repo.issues.items()) if n != SPEC]
    assert len(made) == 2

    for issue in made:
        markdown = tickets.render(implement_spec.spec_ticket(
            conn, github, REPO, github.get_issue(REPO, issue.number), workflow(conn)))

        assert markdown.startswith(f"# {issue.title}\n")
        assert "## Spec exploration notes" in markdown
        assert NOTES_FINDINGS in markdown
        assert markdown.index("## Acceptance criteria") < markdown.index(
            "## Spec exploration notes")
