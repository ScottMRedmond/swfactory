"""Implement-spec's spec PR (orchestrator.md#implement-spec): after the first
ticket lands, one draft PR from the integration branch, whose body lists the
tickets and their status, each linking to its landing comment, and whose
footer closes the spec and every ticket. GitHub's REST API is in-memory
(fake_github.py), its git side a real bare repo (git_repos.py), and runs go
through the fake runner."""

from factory import orchestrator
from test_auto_tickets import ticket
from test_implement_spec import SPEC, workflow
from test_implement_verdicts import (  # noqa: F401 (fixtures)
    REPO, conn, enrolled, fake, github, remote, repo, runner, runs, settings,
)
from test_spec_queue import BRANCH, built_spec, implements, land


def landed_link(issue):
    [comment] = [c for c in issue.posts if "<!-- factory:landed run=" in c["body"]]
    return f"https://github.com/{REPO}/issues/{issue.number}#issuecomment-{comment['id']}"


def build_next(conn, github, remote, runner, settings, files):
    orchestrator.run_once(conn, github, runner, settings)
    [row] = implements(conn, "dispatched")
    return land(conn, github, remote, runner, settings, row, files)


def test_no_spec_pr_before_a_ticket_lands(conn, github, repo, remote, runner, settings):
    built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    orchestrator.run_once(conn, github, runner, settings)

    assert repo.pulls == {}
    assert workflow(conn)["pr"] is None


def test_the_first_landing_opens_a_draft_spec_pr_from_the_integration_branch(
    conn, github, repo, remote, runner, settings
):
    spec, [first, second] = built_spec(conn, github, repo, runner, settings,
                                       ticket("Store saved searches"),
                                       ticket("Show saved searches", [0]))

    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})

    [pr] = repo.pulls.values()
    assert (pr["head"], pr["base"], pr["draft"]) == (BRANCH, "main", True)
    assert pr["title"] == spec.title
    assert workflow(conn)["pr"] == pr["number"]
    body = pr["body"]
    assert f"#{first.number}" in body and f"#{second.number}" in body
    assert landed_link(first) in body
    footer = body.split("\n---\n")[-1]
    for number in (SPEC, first.number, second.number):
        assert f"Closes #{number}" in footer
    assert "agent:in-progress" not in repo.issues[pr["number"]].labels


def test_each_landing_updates_the_list_and_keeps_the_one_pr(
    conn, github, repo, remote, runner, settings
):
    _, [first, second] = built_spec(conn, github, repo, runner, settings,
                                    ticket("Store saved searches"),
                                    ticket("Show saved searches", [0]))
    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    [pr] = repo.pulls.values()
    assert landed_link(first) in pr["body"]
    second_line = next(line for line in pr["body"].splitlines() if f"#{second.number}" in line)
    assert "landed" not in second_line

    build_next(conn, github, remote, runner, settings, {"src/show.py": "show()\n"})

    assert list(repo.pulls.values()) == [pr]
    assert landed_link(first) in pr["body"] and landed_link(second) in pr["body"]


def test_a_ticket_closed_by_hand_is_listed_as_such_and_not_closed_by_the_pr(
    conn, github, repo, remote, runner, settings
):
    _, [first, second] = built_spec(conn, github, repo, runner, settings,
                                    ticket("Store saved searches"),
                                    ticket("Show saved searches"))
    second.state = "closed"

    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})

    [pr] = repo.pulls.values()
    line = next(line for line in pr["body"].splitlines() if f"#{second.number}" in line)
    assert "closed by hand" in line
    footer = pr["body"].split("\n---\n")[-1]
    assert f"Closes #{first.number}" in footer and f"Closes #{second.number}" not in footer


def test_landing_again_after_a_crash_reuses_the_spec_pr(
    conn, github, repo, remote, runner, settings
):
    _, [first] = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    row = build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    [pr] = repo.pulls.values()
    with conn:
        conn.execute("UPDATE runs SET handled_at = NULL WHERE id = ?", (row["id"],))
        conn.execute("UPDATE workflows SET pr = NULL")

    orchestrator.handle_finished(conn, github, runner, settings)

    assert list(repo.pulls.values()) == [pr]
    assert workflow(conn)["pr"] == pr["number"]


def test_the_specs_status_comment_links_the_spec_pr(conn, github, repo, remote, runner, settings):
    spec, _ = built_spec(conn, github, repo, runner, settings, ticket("Store saved searches"))
    build_next(conn, github, remote, runner, settings, {"src/saved.py": "store()\n"})
    orchestrator.run_once(conn, github, runner, settings)

    [pr] = repo.pulls.values()
    [status] = [c for c in spec.comments if "<!-- factory:status -->" in c["body"]]
    assert f"Spec PR: #{pr['number']}" in status["body"]
