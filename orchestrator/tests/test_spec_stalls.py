"""Implement-spec's end and its edge cases (orchestrator.md#implement-spec):
one review of the spec PR once every open ticket is built, a stall notice
when nothing can move, a ticket closed by hand, the spec PR closed without
merging, and the spec edited after decomposition. GitHub's REST API is
in-memory (fake_github.py), its git side a real bare repo (git_repos.py),
and runs go through the fake runner."""

import json

from git_repos import git

from factory import loop, orchestrator
from factory.runner import RunFiles
from test_auto_tickets import ticket
from test_implement_spec import SPEC, workflow
from test_implement_verdicts import (  # noqa: F401 (fixtures)
    REPO, conn, enrolled, fake, github, remote, repo, runner, runs, settings,
)
from test_review_verdicts import finish_review, review_result
from test_spec_pr import build_next
from test_spec_queue import BRANCH, built_spec, implement_result, implements


def reviews(conn, status=None):
    return [r for r in runs(conn) if r["task"] == "review"
            and (status is None or r["status"] == status)]


# --- the review -------------------------------------------------------------------------


def test_once_every_open_ticket_is_built_review_runs_once_on_the_spec_pr(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches", [0]))
    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    assert reviews(conn) == []
    build_next(conn, github, remote, runner, settings, {"src/show.py": "show()\n"})

    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    [pr] = repo.pulls.values()
    [row] = reviews(conn)
    assert row["workflow_id"] == workflow(conn)["id"] and row["ticket"] == str(SPEC)
    sent = json.loads(row["dispatch"])
    assert (sent["pr_number"], sent["branch"], sent["findings_only"]) == (
        pr["number"], BRANCH, False)
    assert sent["base_sha"] == remote.remote_branch(BRANCH)
    markdown = sent["ticket_markdown"]
    for issue in (spec, first, second):
        assert f"## Reviewed against: #{issue.number} {issue.title}" in markdown
    assert markdown.count("## Acceptance criteria") == 2


def all_built(conn, github, repo, remote, runner, settings):
    """A spec of two tickets, both landed. Returns (spec, tickets, the spec PR)."""
    spec, made = built_spec(conn, github, repo, runner, settings,
                            ticket("Store saved searches"), ticket("Show saved searches", [0]))
    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    build_next(conn, github, remote, runner, settings, {"src/show.py": "show()\n"})
    [pr] = repo.pulls.values()
    return spec, made, pr


def spec_review_dispatched(conn, github, repo, remote, runner, settings):
    spec, made, pr = all_built(conn, github, repo, remote, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)
    [row] = reviews(conn, "dispatched")
    return spec, made, pr, row


def spec_fixes(remote, row):
    sent = json.loads(row["dispatch"])
    git(remote.work, "fetch", "-q", "origin", sent["base_sha"])
    remote.branch(sent["branch"], sent["base_sha"])
    remote.commit({"src/show.py": "show()  # fixed\n"}, "review: standards: name the helper")
    return remote.bundle(sent["branch"], sent["base_sha"])


def test_the_spec_reviews_fixes_are_pushed_and_the_spec_pr_made_ready(
    conn, github, repo, remote, runner, settings
):
    spec, made, pr, row = spec_review_dispatched(conn, github, repo, remote, runner, settings)
    bundle = spec_fixes(remote, row)
    tip = remote.head()

    run = finish_review(conn, github, runner, settings, row,
                        review_result(row, spec="changes-requested"), bundle)

    assert run["outcome"] == "succeeded"
    assert remote.remote_branch(BRANCH) == tip
    pr_issue = repo.issues[pr["number"]]
    [verdict] = [c for c in pr_issue.posts if c["body"].startswith("<!-- factory:review ")]
    assert "### Spec: `changes-requested`" in verdict["body"]
    assert pr["draft"] is False and pr["requested_reviewers"] == ["alice"]
    assert "needs-a-human" in pr_issue.labels
    assert workflow(conn)["status"] == "done" and "agent:in-progress" not in spec.labels
    orchestrator.run_once(conn, github, runner, settings)
    assert len(reviews(conn)) == 1


# --- stalls -----------------------------------------------------------------------------


def needs_a_human(conn, github, runner, settings, row):
    runner.finish(row["runner_run_id"], RunFiles(json.dumps(implement_result(
        row, "needs-a-human", comment="Which store?")).encode()))
    orchestrator.run_once(conn, github, runner, settings)


def stall_notices(spec):
    return [c for c in spec.posts if "stalled" in c["body"]]


def test_a_spec_with_nothing_buildable_or_in_flight_stops_with_one_notice_naming_the_tickets(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches", [0]))
    orchestrator.run_once(conn, github, runner, settings)
    [row] = implements(conn)
    needs_a_human(conn, github, runner, settings, row)

    for _ in range(3):
        orchestrator.run_once(conn, github, runner, settings)

    assert workflow(conn)["status"] == "stopped"
    assert "agent:in-progress" not in spec.labels
    [notice] = stall_notices(spec)
    body = notice["body"]
    assert f"#{first.number}" in body and "needs a human" in body
    assert f"#{second.number}" in body
    assert "@alice" in body
    assert "agent:implement" in body


def test_a_cycle_and_a_missing_reference_are_named_in_the_stall_notice(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second, third] = built_spec(conn, github, repo, runner, settings,
                                              ticket("Store saved searches"),
                                              ticket("Show saved searches", [0]),
                                              ticket("Delete saved searches"))
    first.body = first.body.replace("None (can start immediately)", f"- #{second.number}")
    third.body = third.body.replace("None (can start immediately)", "- #999")

    orchestrator.run_once(conn, github, runner, settings)

    assert implements(conn) == []
    assert workflow(conn)["status"] == "stopped"
    [notice] = stall_notices(spec)
    lines = notice["body"].splitlines()
    assert any(f"#{first.number}, #{second.number}" in line and "cycle" in line for line in lines)
    assert any(f"#{third.number}" in line and "#999, which doesn't exist" in line
               for line in lines)


def test_a_ticket_waiting_on_an_open_issue_outside_the_spec_is_not_a_stall(
    conn, github, repo, remote, runner, settings
):
    repo.add_issue(5, "Search index migration")
    spec, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    first.body = first.body.replace("None (can start immediately)", "- #5")

    orchestrator.run_once(conn, github, runner, settings)

    assert workflow(conn)["status"] == "active" and stall_notices(spec) == []


def test_re_adding_agent_implement_to_the_stuck_ticket_sets_the_spec_going_again(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches", [0]))
    orchestrator.run_once(conn, github, runner, settings)
    [row] = implements(conn)
    needs_a_human(conn, github, runner, settings, row)
    orchestrator.run_once(conn, github, runner, settings)
    assert workflow(conn)["status"] == "stopped"

    first.label("agent:implement", by="alice")
    orchestrator.run_once(conn, github, runner, settings)

    assert workflow(conn)["status"] == "active" and "agent:in-progress" in spec.labels
    [_, again] = implements(conn)
    assert again["ticket"] == str(first.number) and again["status"] == "dispatched"
    needs_a_human(conn, github, runner, settings, again)
    orchestrator.run_once(conn, github, runner, settings)
    assert len(stall_notices(spec)) == 2  # a new stall, a new notice


# --- a ticket closed by hand ------------------------------------------------------------


def test_a_ticket_closed_by_hand_stalls_its_dependents_and_shows_on_the_spec_pr(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second, third] = built_spec(conn, github, repo, runner, settings,
                                              ticket("Store saved searches"),
                                              ticket("Show saved searches"),
                                              ticket("Delete saved searches", [1]))
    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    [pr] = repo.pulls.values()
    second.state = "closed"

    orchestrator.run_once(conn, github, runner, settings)

    line = next(x for x in pr["body"].splitlines() if f"#{second.number}" in x)
    assert "closed by hand" in line
    assert f"Closes #{second.number}" not in pr["body"]
    assert workflow(conn)["status"] == "stopped"
    [notice] = stall_notices(spec)
    body = notice["body"]
    assert f"#{third.number}" in body and f"#{second.number}, which was closed by hand" in body
    assert "reopen it" in body and "## Blocked by" in body


def test_a_ticket_closed_by_hand_is_not_reviewed(conn, github, repo, remote, runner, settings):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches"))
    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    second.state = "closed"

    orchestrator.run_once(conn, github, runner, settings)

    [row] = reviews(conn)
    markdown = json.loads(row["dispatch"])["ticket_markdown"]
    assert f"## Reviewed against: #{first.number}" in markdown
    assert f"## Reviewed against: #{second.number}" not in markdown


def test_the_spec_pr_list_follows_a_ticket_closed_by_hand_while_others_build(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second, third] = built_spec(conn, github, repo, runner, settings,
                                              ticket("Store saved searches"),
                                              ticket("Show saved searches"),
                                              ticket("Delete saved searches"))
    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    [pr] = repo.pulls.values()
    third.state = "closed"

    orchestrator.run_once(conn, github, runner, settings)  # the second is building

    assert implements(conn, "dispatched")[0]["ticket"] == str(second.number)
    line = next(x for x in pr["body"].splitlines() if f"#{third.number}" in x)
    assert "closed by hand" in line


# --- the spec PR closed -----------------------------------------------------------------


def close_pr(repo, pr, merged=False):
    pr["state"] = "closed"
    pr["merged"] = merged
    repo.issues[pr["number"]].state = "closed"


def test_closing_the_spec_pr_without_merging_cancels_every_run_and_keeps_the_branch(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches"))
    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    orchestrator.run_once(conn, github, runner, settings)
    [building] = implements(conn, "dispatched")
    assert building["ticket"] == str(second.number)
    [pr] = repo.pulls.values()
    head = remote.remote_branch(BRANCH)

    close_pr(repo, pr)
    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    assert loop.get_run(conn, building["id"])["outcome"] == "cancelled"
    assert [r for r in runs(conn) if r["status"] != "done"] == []
    assert workflow(conn)["status"] == "cancelled"
    assert remote.remote_branch(BRANCH) == head
    assert "agent:in-progress" not in spec.labels and "agent:in-progress" not in second.labels
    closed = f"#{pr['number']}, was closed without merging"
    [notice] = [c for c in spec.posts if closed in c["body"]]
    body = notice["body"]
    assert f"`{BRANCH}`" in body and f"`spec:{SPEC}`" in body
    assert "`agent:implement-spec`" in body and "@alice" in body


def test_closing_the_spec_pr_cancels_its_review(conn, github, repo, remote, runner, settings):
    spec, _, pr, row = spec_review_dispatched(conn, github, repo, remote, runner, settings)

    close_pr(repo, pr)
    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    assert loop.get_run(conn, row["id"])["outcome"] == "cancelled"
    assert workflow(conn)["status"] == "cancelled"
    assert len([c for c in spec.posts if "closed without merging" in c["body"]]) == 1


def test_a_merged_spec_pr_cancels_nothing(conn, github, repo, remote, runner, settings):
    spec, made, pr, row = spec_review_dispatched(conn, github, repo, remote, runner, settings)
    finish_review(conn, github, runner, settings, row, review_result(row))

    close_pr(repo, pr, merged=True)
    for issue in (spec, *made):
        issue.state = "closed"
    orchestrator.run_once(conn, github, runner, settings)

    assert workflow(conn)["status"] == "done"
    assert not [c for c in spec.posts if "closed without merging" in c["body"]]


def test_agent_review_on_the_spec_pr_reviews_it_again_against_the_spec(
    conn, github, repo, remote, runner, settings
):
    repo.permissions["bob"] = "write"
    spec, [first, second], pr, row = spec_review_dispatched(conn, github, repo, remote, runner,
                                                            settings)
    finish_review(conn, github, runner, settings, row, review_result(row))
    repo.issues[pr["number"]].label("agent:review", by="bob", at="2099-01-01T00:00:00Z")

    orchestrator.run_once(conn, github, runner, settings)

    [_, again] = reviews(conn)
    assert again["workflow_id"] == workflow(conn)["id"] and again["status"] == "dispatched"
    sent = json.loads(again["dispatch"])
    assert sent["findings_only"] is False and sent["branch"] == BRANCH
    assert sent["triggered_by"]["login"] == "bob"
    assert f"## Reviewed against: #{second.number}" in sent["ticket_markdown"]
    assert workflow(conn)["status"] == "active" and "agent:in-progress" in spec.labels
    assert "agent:review" not in repo.issues[pr["number"]].labels


# --- the spec edited --------------------------------------------------------------------


def edit_notices(spec):
    return [c for c in spec.posts if "changed after its tickets were made" in c["body"]]


def test_each_change_to_the_specs_body_after_decomposition_posts_one_notice(
    conn, github, repo, remote, runner, settings
):
    spec, _ = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"),
                         ticket("Show saved searches"))
    orchestrator.run_once(conn, github, runner, settings)
    spec.labels.append("enhancement")  # a label isn't an edit
    orchestrator.run_once(conn, github, runner, settings)
    assert edit_notices(spec) == []

    spec.body += "\nAlso share saved searches.\n"
    for _ in range(2):
        orchestrator.run_once(conn, github, runner, settings)
    assert len(edit_notices(spec)) == 1

    spec.body += "\nAnd export them.\n"
    orchestrator.run_once(conn, github, runner, settings)
    [_, notice] = edit_notices(spec)
    assert "edit the tickets to match" in notice["body"]


def test_a_change_to_a_stalled_specs_body_posts_one_notice(
    conn, github, repo, remote, runner, settings
):
    spec, _ = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"),
                         ticket("Show saved searches", [0]))
    orchestrator.run_once(conn, github, runner, settings)
    [row] = implements(conn)
    needs_a_human(conn, github, runner, settings, row)
    orchestrator.run_once(conn, github, runner, settings)
    assert workflow(conn)["status"] == "stopped"

    spec.body += "\nAlso share saved searches.\n"
    for _ in range(2):
        orchestrator.run_once(conn, github, runner, settings)

    assert len(edit_notices(spec)) == 1
    assert workflow(conn)["status"] == "stopped"
    assert len(stall_notices(spec)) == 1
