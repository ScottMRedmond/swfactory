"""Talking to GitHub as the App, against an in-memory GitHub (fake_github.py)."""

import logging

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory.github import AppAuth, GitHub, GitHubError


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(clock):
    fake = FakeGitHub(clock=clock)
    fake.add_repo("octo/widgets")
    fake.add_repo("octo/gadgets")
    return fake


@pytest.fixture
def github(fake, clock):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=clock), fake)


def test_a_token_is_scoped_to_the_one_repo_it_was_fetched_for(fake, clock):
    auth = AppAuth(APP_ID, PRIVATE_KEY, fake, clock=clock)

    token = auth.token("octo/widgets")

    assert fake.tokens[token]["repos"] == ["octo/widgets"]


def test_a_token_is_reused_until_shortly_before_it_expires(fake, clock):
    auth = AppAuth(APP_ID, PRIVATE_KEY, fake, clock=clock)
    first = auth.token("octo/widgets")

    clock.now += 50 * 60
    assert auth.token("octo/widgets") == first

    clock.now += 6 * 60
    assert auth.token("octo/widgets") != first


def test_each_repo_gets_its_own_token(fake, clock):
    auth = AppAuth(APP_ID, PRIVATE_KEY, fake, clock=clock)

    assert auth.token("octo/widgets") != auth.token("octo/gadgets")


def test_a_repo_the_app_isnt_installed_on_is_an_error_without_the_jwt(fake, clock):
    auth = AppAuth(APP_ID, PRIVATE_KEY, fake, clock=clock)

    with pytest.raises(GitHubError) as e:
        auth.token("octo/elsewhere")

    assert e.value.status == 404
    assert "Bearer" not in str(e.value)


def test_tokens_never_reach_the_logs_or_reprs(github, fake, caplog):
    caplog.set_level(logging.DEBUG)

    github.default_branch_head("octo/widgets")

    token = next(iter(fake.tokens))
    assert token not in caplog.text
    assert all(token not in repr(r) for r in fake.requests)
    assert token not in repr(github)


def test_default_branch_head_is_the_default_branchs_commit(github, fake):
    fake.repos["octo/widgets"].default_branch = "trunk"
    fake.repos["octo/widgets"].head_sha = "a" * 40

    assert github.default_branch_head("octo/widgets") == "a" * 40


def test_open_issues_with_a_label_are_listed_across_pages(github, fake):
    repo = fake.repos["octo/widgets"]
    for n in range(1, 131):
        repo.add_issue(n, f"Issue {n}", labels=["agent:explore"] if n % 2 else [])
    repo.add_issue(200, "Closed", labels=["agent:explore"], state="closed")

    issues = github.open_issues_with_label("octo/widgets", "agent:explore")

    assert sorted(i["number"] for i in issues) == list(range(1, 131, 2))


def test_removing_a_label_takes_it_off_the_issue(github, fake):
    issue = fake.repos["octo/widgets"].add_issue(7, "T", labels=["agent:explore", "bug"])

    github.remove_label("octo/widgets", 7, "agent:explore")

    assert issue.labels == ["bug"]


def test_removing_a_label_thats_already_gone_is_fine(github, fake):
    fake.repos["octo/widgets"].add_issue(7, "T", labels=["bug"])

    github.remove_label("octo/widgets", 7, "agent:explore")


def test_a_comment_is_posted_on_the_issue(github, fake):
    issue = fake.repos["octo/widgets"].add_issue(7, "T")

    github.post_comment("octo/widgets", 7, "Hello")

    assert [c["body"] for c in issue.comments] == ["Hello"]


def test_an_issue_is_fetched_by_number(github, fake):
    fake.repos["octo/widgets"].add_issue(7, "Title", "Body")

    issue = github.get_issue("octo/widgets", 7)

    assert (issue["title"], issue["body"]) == ("Title", "Body")


def test_an_error_answer_raises_with_its_status(github, fake):
    with pytest.raises(GitHubError) as e:
        github.get_issue("octo/widgets", 99)

    assert e.value.status == 404
