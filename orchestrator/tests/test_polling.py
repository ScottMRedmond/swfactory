"""Polling GitHub cheaply and resiliently (orchestrator.md#triggers).

Each repo gets one conditional `issues?since=` call per poll; startup and
waking from sleep get a full scan; errors back off per repo. GitHub is
in-memory (fake_github.py), and time is a clock the tests move by hand.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory import db, orchestrator
from factory.github import AppAuth, GitHub, Response
from factory.runner import FakeRunner

REPO = "octo/widgets"
START = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = START):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake(clock):
    fake = FakeGitHub(clock=clock)
    fake.add_repo(REPO)
    return fake


@pytest.fixture
def repo(fake):
    return fake.repos[REPO]


@pytest.fixture
def github(fake, clock):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=clock), fake)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def settings():
    return orchestrator.Settings(repos=[REPO])


@pytest.fixture
def poller(clock):
    return orchestrator.Poller(clock=clock)


def issue_lists(fake):
    """The query of each issue-list call, in order."""
    return [
        {k: v[0] for k, v in parse_qs(urlsplit(r.url).query).items()}
        for r in fake.requests
        if r.method == "GET" and urlsplit(r.url).path.endswith("/issues")
    ]


def queued(conn):
    return [r["ticket"] for r in conn.execute("SELECT ticket FROM runs ORDER BY id")]


def test_after_the_startup_scan_each_poll_is_one_since_call_and_a_304_changes_nothing(
    conn, fake, repo, github, runner, settings, poller, clock
):
    repo.add_issue(1, "Old", labels=["bug"])
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    [scan] = issue_lists(fake)
    assert scan["state"] == "open"

    clock.advance(60)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    clock.advance(60)
    fake.requests.clear()
    orchestrator.run_once(conn, github, runner, settings, poller=poller)

    [call] = [r for r in fake.requests if "/issues" in r.url]
    query = issue_lists(fake)[0]
    assert (query["state"], query["sort"]) == ("all", "updated")
    # The cursor is the start of the last poll that got news, less the 2-minute overlap.
    assert query["since"] == "2027-01-15T07:59:00Z"
    assert call.headers.get("If-None-Match")
    assert queued(conn) == []


def test_a_304_keeps_the_cursor_and_etag(conn, fake, repo, runner, settings, poller, clock):
    statuses = []

    def recording(request):
        response = fake(request)
        if urlsplit(request.url).path.endswith("/issues"):
            statuses.append((parse_qs(urlsplit(request.url).query)["since"][0]
                             if "since=" in request.url else None, response.status))
        return response

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, recording, clock=clock), recording)
    for _ in range(4):
        orchestrator.run_once(conn, github, runner, settings, poller=poller)
        clock.advance(60)

    assert statuses == [
        (None, 200),  # the startup scan
        ("2027-01-15T07:58:00Z", 200),  # a fresh query: no ETag for it yet
        ("2027-01-15T07:59:00Z", 304),
        ("2027-01-15T07:59:00Z", 304),
    ]


def test_a_label_added_between_polls_is_found_by_the_since_call(
    conn, fake, repo, github, runner, settings, poller, clock
):
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    clock.advance(60)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)

    clock.advance(30)
    issue = repo.add_issue(7, "New", labels=["agent:explore"])
    clock.advance(30)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)

    assert queued(conn) == ["7"]
    assert issue.labels == []
    assert [q["state"] for q in issue_lists(fake)] == ["open", "all", "all"]


def test_a_closed_issue_with_the_label_starts_nothing(
    conn, repo, github, runner, settings, poller, clock
):
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    repo.add_issue(7, "Done", labels=["agent:explore"], state="closed")
    clock.advance(60)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)

    assert queued(conn) == []


def full_scans(fake):
    return [q for q in issue_lists(fake) if q["state"] == "open"]


def test_a_gap_of_more_than_5_minutes_brings_a_full_scan_with_fresh_tokens(
    conn, fake, repo, github, runner, settings, poller, clock
):
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    clock.advance(60)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    tokens_before = len(fake.tokens)

    clock.advance(5 * 60 + 1)  # asleep; the cached token is still good for 50 minutes
    orchestrator.run_once(conn, github, runner, settings, poller=poller)

    assert len(full_scans(fake)) == 2
    assert len(fake.tokens) == tokens_before + 1

    clock.advance(60)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    assert len(full_scans(fake)) == 2, "back to the cheap poll"


def test_a_gap_of_exactly_5_minutes_keeps_the_cheap_poll(
    conn, fake, github, runner, settings, poller, clock
):
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    clock.advance(5 * 60)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)

    assert len(full_scans(fake)) == 1


def test_a_trigger_added_while_asleep_is_found(conn, repo, github, runner, settings, poller, clock):
    orchestrator.run_once(conn, github, runner, settings, poller=poller)
    repo.add_issue(7, "While asleep", labels=["agent:explore"], updated_at=clock() + 10)
    clock.advance(3 * 3600)
    orchestrator.run_once(conn, github, runner, settings, poller=poller)

    assert queued(conn) == ["7"]


# --- errors and backoff ---------------------------------------------------------


def failing(fake, response_for):
    """A transport that answers issue-list calls with `response_for(request)`
    when it returns a response, and passes everything else to the fake."""

    def transport(request):
        if request.method == "GET" and urlsplit(request.url).path.endswith("/issues"):
            fake.requests.append(request)
            response = response_for(request)
            if response is not None:
                return response
            fake.requests.pop()
        return fake(request)

    return transport


def github_over(transport, clock):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=clock), transport)


def poll_times(conn, github, runner, settings, poller, fake, clock, seconds, step=60):
    """Run a pass every `step` seconds for `seconds`; return when (since START)
    each issue-list call was made."""
    times = []
    for _ in range(int(seconds // step)):
        before = len(issue_lists(fake))
        orchestrator.run_once(conn, github, runner, settings, poller=poller)
        if len(issue_lists(fake)) > before:
            times.append(clock() - START)
        clock.advance(step)
    return times


def gaps(times):
    return [b - a for a, b in zip(times, times[1:])]


def test_5xx_errors_double_the_wait_up_to_15_minutes_and_one_success_resets_it(
    conn, fake, clock, runner, settings, poller
):
    down = {"on": True}
    github = github_over(failing(fake, lambda r: Response(502, {}, b"") if down["on"] else None),
                         clock)

    times = poll_times(conn, github, runner, settings, poller, fake, clock, 60 * 60)
    assert gaps(times)[:4] == [120, 240, 480, 900]
    assert set(gaps(times)[4:]) == {900}

    down["on"] = False
    clock.t = START + times[-1] + 900
    later = poll_times(conn, github, runner, settings, poller, fake, clock, 4 * 60)
    assert gaps(later) == [60, 60, 60]


def test_a_network_error_backs_off_like_a_5xx(conn, fake, clock, runner, settings, poller):
    def refuse(request):
        raise ConnectionRefusedError("connection refused")

    github = github_over(failing(fake, refuse), clock)

    times = poll_times(conn, github, runner, settings, poller, fake, clock, 8 * 60)

    assert times == [0, 120, 360]


def test_one_failing_repo_backs_off_alone(conn, clock, runner, poller):
    fake = FakeGitHub(clock=clock)
    fake.add_repo("octo/broken", installation_id=1)
    fake.add_repo(REPO, installation_id=2)
    github = github_over(
        failing(fake, lambda r: Response(500, {}, b"") if "/octo/broken/" in r.url else None),
        clock,
    )
    settings = orchestrator.Settings(repos=["octo/broken", REPO])

    for _ in range(3):
        orchestrator.run_once(conn, github, runner, settings, poller=poller)
        clock.advance(60)

    polled = [urlsplit(r.url).path for r in fake.requests
              if urlsplit(r.url).path.endswith("/issues")]
    assert polled.count(f"/repos/{REPO}/issues") == 3
    assert polled.count("/repos/octo/broken/issues") == 2


@pytest.mark.parametrize(
    "status, headers, wait",
    [
        (429, {"Retry-After": "600"}, 600),
        (403, {"Retry-After": "90"}, 90),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(START) + 1500)}, 1500),
        (429, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(START) + 2400)}, 2400),
    ],
)
def test_a_rate_limit_waits_as_long_as_github_says(
    conn, fake, clock, runner, settings, poller, status, headers, wait
):
    limited = {"on": True}

    def answer(request):
        if limited["on"]:
            limited["on"] = False
            return Response(status, headers, b'{"message": "API rate limit exceeded"}')
        return None

    github = github_over(failing(fake, answer), clock)

    times = poll_times(conn, github, runner, settings, poller, fake, clock, wait + 120, step=30)

    assert times[:2] == [0, wait]


def test_a_403_that_isnt_a_rate_limit_backs_off_like_an_error(
    conn, fake, clock, runner, settings, poller
):
    github = github_over(failing(fake, lambda r: Response(403, {}, b'{"message": "Forbidden"}')),
                         clock)

    times = poll_times(conn, github, runner, settings, poller, fake, clock, 6 * 60)

    assert times == [0, 120]


def test_after_30_minutes_of_failures_it_logs_an_error_once(
    conn, fake, clock, runner, settings, poller, caplog
):
    github = github_over(failing(fake, lambda r: Response(503, {}, b"")), clock)

    with caplog.at_level("WARNING", logger="factory.orchestrator"):
        poll_times(conn, github, runner, settings, poller, fake, clock, 29 * 60)
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
        poll_times(conn, github, runner, settings, poller, fake, clock, 60 * 60)

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert REPO in errors[0] and "failed for" in errors[0]
