"""`factory serve`: the orchestrator's main loop, against an in-memory GitHub."""

import json
import time

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory import cli, db, orchestrator, serve
from factory.contract import parse_result
from factory.github import AppAuth, GitHub
from factory.runner import CannedRunner, RunState

REPO = "octo/widgets"


class Stop(Exception):
    pass


def test_serve_runs_a_pass_about_every_60_seconds(tmp_path):
    fake = FakeGitHub()
    issue = fake.add_repo(REPO).add_issue(7, "T", labels=["agent:explore"])
    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake)
    conn = db.connect(tmp_path / "factory.db")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    with pytest.raises(Stop):
        orchestrator.serve(conn, github, CannedRunner(), orchestrator.Settings(repos=[REPO]),
                           sleep=sleep)

    assert sleeps == [60, 60, 60]
    assert len(issue.comments) == 1


def test_the_canned_runner_finishes_each_run_with_a_valid_result():
    runner = CannedRunner({"verdict": "waiting", "summary": "S", "findings": "F",
                           "plan": [], "files": [], "questions": ["Q?"]})

    rid = runner.dispatch(REPO, {"run_id": 12, "task": "explore"})

    assert runner.state(REPO, rid) is RunState.FINISHED
    result = parse_result(json.loads(runner.files(REPO, rid).result_json), task="explore")
    assert (result.run_id, result.output.verdict) == (12, "waiting")


def test_serve_needs_the_app_id_and_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FACTORY_APP_ID", raising=False)
    monkeypatch.delenv("FACTORY_APP_KEY_FILE", raising=False)

    code = cli.main(["--db", str(tmp_path / "f.db"), "serve", "--repo", REPO, "--fake-runner"])

    assert code == 2
    assert "FACTORY_APP_ID" in capsys.readouterr().err


def test_serve_wires_the_app_the_repos_and_the_fake_runner(tmp_path, monkeypatch):
    key = tmp_path / "app.pem"
    key.write_text(PRIVATE_KEY)
    output = tmp_path / "output.json"
    output.write_text(json.dumps({"verdict": "easy-call", "summary": "S", "findings": "F",
                                  "plan": ["p"], "files": ["f"]}))
    monkeypatch.setenv("FACTORY_APP_ID", APP_ID)
    monkeypatch.setenv("FACTORY_APP_KEY_FILE", str(key))
    fake = FakeGitHub(clock=time.time)  # the CLI signs its JWTs with the real clock
    issue = fake.add_repo(REPO).add_issue(7, "T", labels=["agent:explore"])
    monkeypatch.setattr(serve, "TRANSPORT", fake)
    sleeps = []

    def sleep(seconds):  # pass 1 queues and dispatches, pass 2 collects and posts
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Stop

    monkeypatch.setattr(serve, "SLEEP", sleep)

    with pytest.raises(Stop):
        cli.main(["--db", str(tmp_path / "f.db"), "--logs", str(tmp_path / "logs"),
                  "serve", "--repo", REPO, "--fake-runner", "--fake-output", str(output)])

    assert "**Verdict:** `easy-call`" in issue.comments[0]["body"]
    logged = [json.loads(line) for line in (tmp_path / "logs" / "factory.log").read_text().splitlines()]
    posted = [line for line in logged if line["msg"].startswith("posted explore findings")]
    assert [(line["run"], line["repo"], line["ticket"]) for line in posted] == [(1, REPO, "7")]


def test_serve_refuses_to_run_without_a_runner(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_APP_ID", APP_ID)
    monkeypatch.setenv("FACTORY_APP_KEY_FILE", str(tmp_path / "missing.pem"))

    code = cli.main(["--db", str(tmp_path / "f.db"), "serve", "--repo", REPO])

    assert code == 2
    assert "--fake-runner" in capsys.readouterr().err
