"""Enrolment discovery: which repos are polled, and how each enrolment is applied.

GitHub is in-memory (fake_github.py): `fake.add_repo` installs the App on a
repo, and a repo is enrolled by putting `.factory/config.yml` in its files.
"""

import json

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory import cli, db, enrolment, loop, orchestrator
from factory.contract import parse_dispatch
from factory.github import AppAuth, GitHub, Response
from factory.runner import FakeRunner, RunFiles

HEAD = "e" * 40
CONFIG = "setup: uv sync\ncheck: uv run pytest\n"
FACTORY_REPO = "octo/factory"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def github(fake):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def settings():
    return orchestrator.Settings(factory_repo=FACTORY_REPO)


def enrolled_repo(fake, name, config=CONFIG, **kw):
    repo = fake.add_repo(name, head_sha=HEAD, **kw)
    repo.files[".factory/config.yml"] = config
    return repo


def runs(conn):
    return conn.execute("SELECT * FROM runs ORDER BY id").fetchall()


def polled(conn):
    return [e.repo for e in enrolment.polled(conn)]


# --- which repos are polled ---------------------------------------------------------


def test_an_installed_repo_with_a_valid_config_is_polled_and_one_without_isnt(
    conn, fake, github, settings
):
    enrolled_repo(fake, "octo/widgets")
    fake.add_repo("octo/gadgets")

    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    assert polled(conn) == ["octo/widgets"]


def test_only_enrolled_repos_are_polled_for_triggers(conn, fake, github, runner, settings):
    widget = enrolled_repo(fake, "octo/widgets").add_issue(1, "T", labels=["agent:explore"])
    gadget = fake.add_repo("octo/gadgets").add_issue(2, "T", labels=["agent:explore"])
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    orchestrator.run_once(conn, github, runner, settings)

    assert [(r["repo"], r["ticket"]) for r in runs(conn)] == [("octo/widgets", "1")]
    assert widget.labels == []
    assert gadget.labels == ["agent:explore"]


def test_a_config_that_fails_the_schema_isnt_polled(conn, fake, github, settings):
    enrolled_repo(fake, "octo/widgets", config="setup: uv sync\n")  # no check

    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    assert polled(conn) == []


# --- labels -------------------------------------------------------------------------


def test_the_first_poll_creates_missing_factory_labels_and_leaves_existing_ones_alone(
    conn, fake, github, runner, settings
):
    repo = enrolled_repo(fake, "octo/widgets")
    repo.labels["agent:explore"] = {"name": "agent:explore", "color": "000000",
                                    "description": "ours"}
    repo.labels["bug"] = {"name": "bug", "color": "d73a4a", "description": ""}
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    orchestrator.run_once(conn, github, runner, settings)

    assert {"agent:explore", "agent:implement", "agent:review", "agent:implement-spec",
            "agent:in-progress", "agent:blocked", "agent:built", "needs-a-human", "waiting",
            "spec", "bug"} == set(repo.labels)
    assert repo.labels["agent:explore"] == {"name": "agent:explore", "color": "000000",
                                            "description": "ours"}


def test_labels_are_only_created_on_the_first_poll(conn, fake, github, runner, settings):
    repo = enrolled_repo(fake, "octo/widgets")
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)
    orchestrator.run_once(conn, github, runner, settings)
    del repo.labels["waiting"]  # a human deletes one they don't want

    orchestrator.run_once(conn, github, runner, settings)
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)
    orchestrator.run_once(conn, github, runner, settings)

    assert "waiting" not in repo.labels


def test_labels_that_couldnt_be_created_are_tried_again_on_the_next_poll(
    conn, fake, runner, settings
):
    repo = enrolled_repo(fake, "octo/widgets")
    broken = {"on": True}

    def transport(request):
        if broken["on"] and request.method == "POST" and request.url.endswith("/labels"):
            return Response(502, {}, b"")
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)
    orchestrator.run_once(conn, github, runner, settings)
    assert repo.labels == {}

    broken["on"] = False
    orchestrator.run_once(conn, github, runner, settings)

    assert "agent:explore" in repo.labels


# --- applying config.yml ------------------------------------------------------------


def queued_dispatch(conn):
    [row] = runs(conn)
    return parse_dispatch(json.loads(row["dispatch"]))


def test_a_dispatch_carries_the_commit_the_enrolment_was_read_at(conn, fake, github, settings):
    repo = enrolled_repo(fake, "octo/widgets")
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)
    repo.head_sha = "f" * 40  # the default branch moves on before the trigger
    repo.add_issue(1, "T", labels=["agent:explore"])

    orchestrator.poll(conn, github, "octo/widgets", settings)

    dispatch = queued_dispatch(conn)
    assert dispatch.enrolment_sha == HEAD
    assert dispatch.base_sha == "f" * 40


def test_the_global_timeout_and_model_apply_without_overrides(conn, fake, github, settings):
    enrolled_repo(fake, "octo/widgets").add_issue(1, "T", labels=["agent:explore"])
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    orchestrator.poll(conn, github, "octo/widgets", settings)

    dispatch = queued_dispatch(conn)
    assert (dispatch.timeout_minutes, dispatch.model) == (20, "claude-sonnet-4-6")


def test_per_task_overrides_replace_the_global_timeout_and_model(conn, fake, github, settings):
    config = CONFIG + "timeouts:\n  explore: 30\nmodels:\n  explore: claude-haiku-4-5\n"
    enrolled_repo(fake, "octo/widgets", config=config).add_issue(1, "T", labels=["agent:explore"])
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    orchestrator.poll(conn, github, "octo/widgets", settings)

    dispatch = queued_dispatch(conn)
    assert (dispatch.timeout_minutes, dispatch.model) == (30, "claude-haiku-4-5")


def test_a_disabled_repo_queues_triggers_but_dispatches_nothing(
    conn, fake, github, runner, settings
):
    repo = enrolled_repo(fake, "octo/widgets", config=CONFIG + "enabled: false\n")
    issue = repo.add_issue(1, "T", labels=["agent:explore"])
    enrolled_repo(fake, "octo/gadgets").add_issue(2, "T", labels=["agent:explore"])
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    orchestrator.run_once(conn, github, runner, settings)

    assert [(r["repo"], r["status"]) for r in runs(conn)] == [
        ("octo/gadgets", "dispatched"), ("octo/widgets", "queued")]
    assert issue.labels == []

    repo.files[".factory/config.yml"] = CONFIG  # enabled again
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)
    orchestrator.run_once(conn, github, runner, settings)

    assert [r["status"] for r in runs(conn)] == ["dispatched", "dispatched"]


# --- unenrolling --------------------------------------------------------------------


def explore_result(run_id):
    from test_explore_trigger import explore_result as result

    return result(run_id)


@pytest.fixture
def busy(conn, fake, github, runner, settings):
    """octo/widgets with one explore in flight, one queued and an active workflow."""
    repo = enrolled_repo(fake, "octo/widgets")
    repo.add_issue(1, "T", labels=["agent:explore"])
    repo.add_issue(2, "T", labels=["agent:explore"])
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)
    orchestrator.run_once(conn, github, FakeRunner(), orchestrator.Settings(max_in_flight=0))
    loop.tick(conn, runner, limit=1)
    conn.execute("""INSERT INTO workflows (repo, kind, subject, status)
                    VALUES ('octo/widgets', 'implement-ticket', '1', 'active'),
                           ('octo/widgets', 'implement-ticket', '9', 'done')""")
    assert [r["status"] for r in runs(conn)] == ["dispatched", "queued"]
    return repo


def workflow_statuses(conn):
    return [r["status"] for r in conn.execute("SELECT status FROM workflows ORDER BY id")]


@pytest.mark.parametrize("how", ["config deleted", "app uninstalled"])
def test_unenrolling_cancels_queued_runs_and_stops_active_workflows(
    conn, fake, github, runner, settings, busy, how
):
    if how == "config deleted":
        del busy.files[".factory/config.yml"]
    else:
        fake.uninstall("octo/widgets")

    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    assert polled(conn) == []
    assert [(r["status"], r["outcome"]) for r in runs(conn)] == [
        ("dispatched", None), ("done", "cancelled")]
    assert workflow_statuses(conn) == ["stopped", "done"]


def test_an_in_flight_run_of_an_unenrolled_repo_finishes_but_posts_nothing(
    conn, fake, github, runner, settings, busy
):
    del busy.files[".factory/config.yml"]
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)
    rid = loop.get_run(conn, 1)["runner_run_id"]
    runner.finish(rid, RunFiles(result_json=json.dumps(explore_result(1)).encode()))

    orchestrator.run_once(conn, github, runner, settings)

    row = loop.get_run(conn, 1)
    assert (row["status"], row["outcome"]) == ("done", "succeeded")
    assert row["result"] is not None
    assert busy.issues[1].comments == []


def test_a_queued_run_of_an_unenrolled_repo_is_never_dispatched(
    conn, fake, github, runner, settings, busy
):
    fake.uninstall("octo/widgets")
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    orchestrator.run_once(conn, github, runner, settings)

    assert len(runner.dispatched) == 1


def test_a_failing_installations_list_changes_nothing(conn, fake, runner, settings, busy):
    def transport(request):
        if request.url.endswith("/installation/repositories?per_page=100"):
            return Response(502, {}, b"")
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)

    with pytest.raises(Exception):
        enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    assert polled(conn) == ["octo/widgets"]
    assert [r["status"] for r in runs(conn)] == ["dispatched", "queued"]


def test_a_config_that_cant_be_read_keeps_the_repo_as_it_was(conn, fake, runner, settings, busy):
    def transport(request):
        if "/contents/" in request.url:
            return Response(500, {}, b"")
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)

    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    assert polled(conn) == ["octo/widgets"]
    assert [r["status"] for r in runs(conn)] == ["dispatched", "queued"]


def test_a_repo_that_enrols_again_is_polled_again(conn, fake, github, runner, settings, busy):
    del busy.files[".factory/config.yml"]
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)
    busy.files[".factory/config.yml"] = CONFIG

    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    assert polled(conn) == ["octo/widgets"]


# --- the factory's owner ------------------------------------------------------------


def test_the_refresh_reads_the_owner_from_the_factory_repos_main(conn, fake, github):
    factory = fake.add_repo(FACTORY_REPO, installation_id=200)
    factory.files["factory-config.yml"] = "owner: octocat\n"

    assert enrolment.refresh(conn, github, factory_repo=FACTORY_REPO) == "octocat"


def test_a_missing_owner_is_none_and_doesnt_stop_the_refresh(conn, fake, github):
    enrolled_repo(fake, "octo/widgets")

    assert enrolment.refresh(conn, github, factory_repo=FACTORY_REPO) is None
    assert polled(conn) == ["octo/widgets"]


# --- factory status -----------------------------------------------------------------


def test_factory_status_lists_each_installed_repo_and_whether_its_polled(
    tmp_path, conn, fake, github, capsys
):
    enrolled_repo(fake, "octo/widgets")
    enrolled_repo(fake, "octo/paused", config=CONFIG + "enabled: false\n")
    enrolled_repo(fake, "octo/broken", config="setup: uv sync\n")
    fake.add_repo("octo/gadgets")
    enrolment.refresh(conn, github, factory_repo=FACTORY_REPO)

    code = cli.main(["--db", str(tmp_path / "factory.db"), "status"])

    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("  octo/")}
    assert code == 0
    assert "enrolled" in lines["octo/widgets"] and "eeeeeee" in lines["octo/widgets"]
    assert "disabled" in lines["octo/paused"]
    assert "invalid" in lines["octo/broken"] and "check" in lines["octo/broken"]
    assert "not enrolled" in lines["octo/gadgets"]


def test_factory_status_before_any_refresh_says_so(tmp_path, conn, capsys):
    code = cli.main(["--db", str(tmp_path / "factory.db"), "status"])

    assert code == 0
    assert "no repos found yet" in capsys.readouterr().out


# --- the refresh cadence ------------------------------------------------------------


class Stop(Exception):
    pass


def test_serve_refreshes_enrolments_at_start_and_every_10_minutes(conn, fake, github, runner):
    clock = {"now": 0.0}
    passes = []

    def sleep(seconds):
        passes.append(seconds)
        clock["now"] += seconds
        if len(passes) == 1:  # enrolled between the first and the next refresh
            enrolled_repo(fake, "octo/gadgets")
        if len(passes) == 11:
            raise Stop

    enrolled_repo(fake, "octo/widgets")
    with pytest.raises(Stop):
        orchestrator.serve(conn, github, runner, orchestrator.Settings(factory_repo=FACTORY_REPO),
                           sleep=sleep, clock=lambda: clock["now"])

    listings = [r for r in fake.requests if r.url.startswith(
        "https://api.github.com/app/installations?")]
    assert len(listings) == 2  # at 0s and at 600s
    assert polled(conn) == ["octo/gadgets", "octo/widgets"]


def test_a_failed_refresh_doesnt_stop_the_loop_and_is_tried_next_pass(conn, fake, runner):
    enrolled_repo(fake, "octo/widgets")
    broken = {"on": True}
    passes = []

    def transport(request):
        if broken["on"] and "/app/installations" in request.url:
            return Response(502, {}, b"")
        return fake(request)

    def sleep(seconds):
        passes.append(seconds)
        broken["on"] = False
        if len(passes) == 2:
            raise Stop

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)
    with pytest.raises(Stop):
        orchestrator.serve(conn, github, runner, orchestrator.Settings(factory_repo=FACTORY_REPO),
                           sleep=sleep, clock=lambda: 0.0)

    assert polled(conn) == ["octo/widgets"]
