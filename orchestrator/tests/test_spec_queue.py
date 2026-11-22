"""Implement-spec's build queue and landing (orchestrator.md#implement-spec):
on each poll the open `spec:<n>` tickets are read again and implement is
queued for the lowest-numbered buildable one, from the integration branch's
head; `built` is landed there with `git merge`. GitHub's REST API is
in-memory (fake_github.py), its git side a real bare repo (git_repos.py), and
runs go through the fake runner."""

import json
from dataclasses import replace

from fake_github import user_id
from git_repos import git

from factory import loop, orchestrator
from factory.runner import RunFiles
from test_auto_tickets import decomposed, ticket
from test_implement_spec import (
    NOTES_FINDINGS, SPEC, auto_tickets_result, dispatched, explored, finish, spec_issue, workflow,
)
from test_implement_verdicts import (  # noqa: F401 (fixtures)
    REPO, conn, enrolled, fake, github, remote, repo, runner, runs, settings,
)

BRANCH = f"factory/spec-{SPEC}"


def built_spec(conn, github, repo, runner, settings, *breakdown):
    """A spec explored and decomposed into `breakdown`. Returns its issue and
    the tickets' issues, in order."""
    spec = spec_issue(repo)
    explored(conn, github, runner, settings, verdict="easy-call")
    orchestrator.run_once(conn, github, runner, settings)
    auto = dispatched(conn, "auto-tickets")
    finish(conn, github, runner, settings, auto, auto_tickets_result(auto["id"],
                                                                     decomposed(*breakdown)))
    made = [i for n, i in sorted(repo.issues.items()) if n > SPEC]
    assert len(made) == len(breakdown)
    return spec, made


def implements(conn, status=None):
    return [r for r in runs(conn) if r["task"] == "implement"
            and (status is None or r["status"] == status)]


def push_to_branch(remote, files, message="A human's commit"):
    """A human pushes to the integration branch."""
    git(remote.work, "fetch", "-q", "origin", BRANCH)
    remote.branch(BRANCH, "FETCH_HEAD")
    remote.commit(files, message)
    git(remote.work, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    return remote.head()


def implement_result(row, verdict="built", **output):
    return {"contract_version": 1, "run_id": row["id"], "task": "implement",
            "outcome": "succeeded", "check": {"passed": True, "output_tail": "ok\n"},
            "output": {"verdict": verdict, **output}}


def commits(remote, row, files, message="Build it"):
    """The agent's commits on the run's branch, from the run's base, bundled
    like the run script does."""
    sent = json.loads(row["dispatch"])
    git(remote.work, "fetch", "-q", "origin", sent["base_sha"])
    remote.branch(sent["branch"], sent["base_sha"])
    remote.commit(files, message)
    return remote.bundle(sent["branch"], sent["base_sha"])


def land(conn, github, remote, runner, settings, row, files, **output):
    """Implement `row` (dispatched) finishes `built` with commits touching `files`."""
    sent = loop.get_run(conn, row["id"])
    output.setdefault("pr_body", f"Built #{sent['ticket']}.")
    runner.finish(row["runner_run_id"], RunFiles(
        json.dumps(implement_result(row, **output)).encode(), commits(remote, sent, files)))
    orchestrator.run_once(conn, github, runner, settings)
    return loop.get_run(conn, row["id"])


# --- the queue --------------------------------------------------------------------------


def test_the_next_poll_after_decomposed_builds_the_first_ticket_on_the_integration_branch(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches", [0]))
    assert implements(conn) == []

    orchestrator.run_once(conn, github, runner, settings)

    [implement] = implements(conn)
    assert implement["status"] == "dispatched"
    assert implement["ticket"] == str(first.number)
    assert implement["workflow_id"] == workflow(conn)["id"]
    assert (implement["triggered_by"], implement["triggered_by_id"]) == ("alice", user_id("alice"))
    sent = runner.dispatched[-1][1]
    assert sent["task"] == "implement"
    assert sent["base_sha"] == remote.remote_branch(BRANCH)
    assert sent["ticket_markdown"].startswith(f"# {first.title}\n")
    assert NOTES_FINDINGS in sent["ticket_markdown"]
    assert sent["triggered_by"] == {"login": "alice", "id": user_id("alice")}
    assert "agent:in-progress" in first.labels and "agent:in-progress" not in second.labels
    assert "agent:in-progress" in spec.labels


# --- landing ----------------------------------------------------------------------------


def test_built_lands_the_commits_on_the_integration_branch_and_labels_the_ticket_built(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches", [0]))
    orchestrator.run_once(conn, github, runner, settings)
    [implement] = implements(conn)
    before = remote.remote_branch(BRANCH)

    land(conn, github, remote, runner, settings, implement, {"src/saved.py": "store()\n"},
         pr_body="Stores saved searches in the search module.")

    head = remote.remote_branch(BRANCH)
    assert git(remote.path, "rev-parse", f"{head}^") == before  # a fast-forward
    assert git(remote.path, "show", f"{head}:src/saved.py") == "store()"
    assert remote.remote_branch(json.loads(implement["dispatch"])["branch"]) is None
    assert [p["head"] for p in repo.pulls.values()] == [BRANCH]  # the spec PR, none of its own
    assert first.state == "open"
    assert "agent:built" in first.labels and "agent:in-progress" not in first.labels
    [landed] = first.posts
    assert "Stores saved searches in the search module." in landed["body"]
    assert f"`{BRANCH}`" in landed["body"]
    assert "the ticket changed" not in landed["body"]
    assert workflow(conn)["status"] == "active" and "agent:in-progress" in spec.labels


def test_landing_again_after_a_crash_changes_nothing(conn, github, repo, remote, runner, settings):
    _, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    orchestrator.run_once(conn, github, runner, settings)
    [implement] = implements(conn)
    land(conn, github, remote, runner, settings, implement, {"src/saved.py": "store()\n"})
    head = remote.remote_branch(BRANCH)
    with conn:
        conn.execute("UPDATE runs SET handled_at = NULL WHERE id = ?", (implement["id"],))

    orchestrator.handle_finished(conn, github, runner, settings)

    assert remote.remote_branch(BRANCH) == head
    assert len(first.posts) == 1 and "agent:built" in first.labels


def test_a_ticket_edited_while_it_was_built_says_so_when_it_lands(
    conn, github, repo, remote, runner, settings
):
    _, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    orchestrator.run_once(conn, github, runner, settings)
    [implement] = implements(conn)
    first.body += "\nAlso keep the sort order.\n"

    land(conn, github, remote, runner, settings, implement, {"src/saved.py": "store()\n"})

    [landed] = first.posts
    assert "the ticket changed after this run started" in landed["body"]


def test_a_ticket_is_built_once_its_blockers_have_landed(
    conn, github, repo, remote, runner, settings
):
    _, [first, second] = built_spec(conn, github, repo, runner, settings,
                                    ticket("Store saved searches"),
                                    ticket("Show saved searches", [0]))
    orchestrator.run_once(conn, github, runner, settings)
    [implement] = implements(conn)
    land(conn, github, remote, runner, settings, implement, {"src/saved.py": "store()\n"})

    orchestrator.run_once(conn, github, runner, settings)

    [_, following] = implements(conn)
    assert following["ticket"] == str(second.number) and following["status"] == "dispatched"
    assert runner.dispatched[-1][1]["base_sha"] == remote.remote_branch(BRANCH)
    assert "agent:in-progress" in second.labels


def test_a_clean_merge_after_a_human_pushed_lands_as_a_merge_commit(
    conn, github, repo, remote, runner, settings
):
    _, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    orchestrator.run_once(conn, github, runner, settings)
    [implement] = implements(conn)
    pushed = push_to_branch(remote, {"docs/notes.md": "a human's notes\n"})

    land(conn, github, remote, runner, settings, implement, {"src/saved.py": "store()\n"})

    head = remote.remote_branch(BRANCH)
    parents = git(remote.path, "rev-list", "--parents", "-n", "1", head).split()[1:]
    assert parents[0] == pushed and len(parents) == 2
    assert git(remote.path, "show", f"{head}:src/saved.py") == "store()"
    assert git(remote.path, "show", f"{head}:docs/notes.md") == "a human's notes"
    assert "agent:built" in first.labels


def test_a_conflict_discards_the_commits_and_builds_the_ticket_again_on_the_new_head(
    conn, github, repo, remote, runner, settings
):
    _, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    orchestrator.run_once(conn, github, runner, settings)
    [implement] = implements(conn)
    pushed = push_to_branch(remote, {"src/saved.py": "a human's version\n"})

    done = land(conn, github, remote, runner, settings, implement, {"src/saved.py": "store()\n"})

    assert done["outcome"] == "superseded"
    assert remote.remote_branch(BRANCH) == pushed
    assert "agent:built" not in first.labels and first.posts == []
    [_, again] = implements(conn)
    assert (again["ticket"], again["attempt"], again["retry_of"]) == (
        str(first.number), 2, implement["id"])
    assert "agent:in-progress" in first.labels
    orchestrator.run_once(conn, github, runner, settings)
    assert runner.dispatched[-1][1]["base_sha"] == pushed


def test_a_ticket_out_of_attempts_is_blocked_while_the_rest_of_the_spec_goes_on(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches"))
    for n in range(2):
        orchestrator.run_once(conn, github, runner, settings)
        [row] = implements(conn, "dispatched")
        assert row["ticket"] == str(first.number)
        push_to_branch(remote, {"src/saved.py": f"a human's version {n}\n"})
        land(conn, github, remote, runner, settings, row, {"src/saved.py": "store()\n"})

    [notice] = first.posts
    assert "gave up on `implement` after 2 attempts" in notice["body"]
    assert "Add `agent:implement` to try again" in notice["body"]
    assert "agent:blocked" in first.labels and "agent:in-progress" not in first.labels
    assert workflow(conn)["status"] == "active" and "agent:in-progress" in spec.labels
    orchestrator.run_once(conn, github, runner, settings)
    [row] = implements(conn, "dispatched")
    assert row["ticket"] == str(second.number)


def test_needs_a_human_labels_the_ticket_and_the_next_buildable_one_goes_ahead(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second, third] = built_spec(conn, github, repo, runner, settings,
                                              ticket("Store saved searches"),
                                              ticket("Show saved searches", [0]),
                                              ticket("Delete saved searches"))
    orchestrator.run_once(conn, github, runner, settings)
    [row] = implements(conn)
    runner.finish(row["runner_run_id"], RunFiles(json.dumps(implement_result(
        row, "needs-a-human", comment="Which store?")).encode()))

    orchestrator.run_once(conn, github, runner, settings)

    [comment] = first.posts
    assert "Which store?" in comment["body"]
    assert "needs-a-human" in first.labels and "agent:in-progress" not in first.labels
    assert workflow(conn)["status"] == "active"
    orchestrator.run_once(conn, github, runner, settings)
    [_, following] = implements(conn)
    assert following["ticket"] == str(third.number)  # the second waits on the first


# --- the queue reads the issues ---------------------------------------------------------


def test_no_more_than_per_spec_concurrency_tickets_are_in_flight(
    conn, github, repo, remote, runner, settings
):
    built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"),
               ticket("Show saved searches"), ticket("Delete saved searches"))

    for _ in range(2):
        orchestrator.run_once(conn, github, runner, settings)
    assert [r["ticket"] for r in implements(conn)] == [str(SPEC + 1)]

    wider = replace(settings, per_spec_concurrency=2)
    orchestrator.run_once(conn, github, runner, wider)
    orchestrator.run_once(conn, github, runner, wider)
    assert [r["ticket"] for r in implements(conn)] == [str(SPEC + 1), str(SPEC + 2)]


def test_a_blocker_outside_the_spec_counts_once_its_closed(
    conn, github, repo, remote, runner, settings
):
    outside = repo.add_issue(5, "Search index migration")
    _, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    first.body = first.body.replace("None (can start immediately)", "- #5")

    orchestrator.run_once(conn, github, runner, settings)
    assert implements(conn) == []

    outside.state = "closed"
    orchestrator.run_once(conn, github, runner, settings)
    [row] = implements(conn)
    assert row["ticket"] == str(first.number)


def test_a_missing_blocker_or_a_spec_ticket_closed_by_hand_never_counts(
    conn, github, repo, remote, runner, settings
):
    _, [first, second, third] = built_spec(conn, github, repo, runner, settings,
                                           ticket("Store saved searches"),
                                           ticket("Show saved searches", [0]),
                                           ticket("Delete saved searches"))
    first.state = "closed"
    first.labels.append("agent:built")
    third.body = third.body.replace("None (can start immediately)", "- #999")

    for _ in range(2):
        orchestrator.run_once(conn, github, runner, settings)

    assert implements(conn) == []


def test_a_ticket_added_by_hand_joins_the_queue(conn, github, repo, remote, runner, settings):
    _, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    first.labels.append("needs-a-human")
    added = repo.add_issue(50, "Export saved searches",
                           f"## Parent\n\n#{SPEC}\n\n## Blocked by\n\nNone\n",
                           labels=[f"spec:{SPEC}"])

    orchestrator.run_once(conn, github, runner, settings)

    [row] = implements(conn)
    assert row["ticket"] == str(added.number)


def test_closing_a_ticket_cancels_its_build_and_leaves_the_spec_going(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches"))
    orchestrator.run_once(conn, github, runner, settings)
    [row] = implements(conn)
    first.state = "closed"

    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    assert loop.get_run(conn, row["id"])["outcome"] == "cancelled"
    assert "agent:in-progress" not in first.labels
    assert workflow(conn)["status"] == "active" and "agent:in-progress" in spec.labels
    assert [r["ticket"] for r in implements(conn, "dispatched")] == [str(second.number)]


# --- agent:implement on a spec ticket ---------------------------------------------------


def stalled(conn, spec):
    """What #34's stall handling leaves: the workflow stopped, the spec not in progress."""
    with conn:
        conn.execute("UPDATE workflows SET status = 'stopped'")
    spec.labels.remove("agent:in-progress")


def test_agent_implement_on_a_stuck_spec_ticket_builds_it_on_the_integration_branch(
    conn, github, repo, remote, runner, settings
):
    repo.permissions["bob"] = "write"
    spec, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    first.labels.append("needs-a-human")
    stalled(conn, spec)
    first.label("agent:implement", by="bob")

    orchestrator.run_once(conn, github, runner, settings)

    [row] = implements(conn)
    assert row["workflow_id"] == workflow(conn)["id"] and row["status"] == "dispatched"
    assert (row["triggered_by"], row["triggered_by_id"]) == ("bob", user_id("bob"))
    sent = runner.dispatched[-1][1]
    assert sent["base_sha"] == remote.remote_branch(BRANCH)
    assert NOTES_FINDINGS in sent["ticket_markdown"]
    assert sorted(first.labels) == ["agent:in-progress", f"spec:{SPEC}"]
    assert workflow(conn)["status"] == "active" and "agent:in-progress" in spec.labels
    assert [w["kind"] for w in conn.execute("SELECT kind FROM workflows")] == ["implement-spec"]


def test_agent_implement_on_a_ticket_still_blocked_waits_for_its_blockers(
    conn, github, repo, remote, runner, settings
):
    _, [first, second] = built_spec(conn, github, repo, runner, settings,
                                    ticket("Store saved searches"),
                                    ticket("Show saved searches", [0]))
    orchestrator.run_once(conn, github, runner, settings)
    second.labels.append("agent:blocked")
    second.label("agent:implement", by="alice")

    orchestrator.run_once(conn, github, runner, settings)

    assert [r["ticket"] for r in implements(conn)] == [str(first.number)]
    assert "agent:blocked" not in second.labels and "agent:implement" not in second.labels
    [notice] = second.posts
    assert f"#{first.number}" in notice["body"]
