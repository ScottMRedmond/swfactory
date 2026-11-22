"""`factory doctor <repo>`: check an enrolment, then prove it with a smoke run."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import example
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub, enrol

from factory import brakes, cli, db, doctor, enrolment, loop
from factory.github import AppAuth, GitHub, Response
from factory.runner import FakeRunner, RunFiles

REPO = "octo/widgets"
# The caller workflow as enrolment copies it.
CALLER = (Path(__file__).resolve().parents[2] / "runner" / "templates" / "factory.yml").read_text()


def healthy(fake: FakeGitHub):
    """A repo that passes every check."""
    repo = enrol(fake.add_repo(REPO))
    repo.files[".factory/Dockerfile"] = "FROM ghcr.io/scottmredmond/factory-base:latest\n"
    repo.files[".github/workflows/factory.yml"] = CALLER
    for name, color, description in enrolment.LABELS:
        repo.labels[name] = {"name": name, "color": color, "description": description}
    return repo


class Doctor:
    """`doctor.run` against an in-memory GitHub and a fake runner.

    The runner finishes the smoke run with `result` on the first wait.
    """

    def __init__(self, tmp_path):
        self.fake = FakeGitHub()
        self.github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, self.fake, clock=self.fake.clock), self.fake)
        self.conn = db.connect(tmp_path / "factory.db")
        self.runner = FakeRunner()
        self.result: dict | None = example("result", "smoke.valid.json")
        self.sleeps: list[float] = []
        self.lines: list[str] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        assert len(self.sleeps) < 100, "doctor never stopped waiting"
        if len(self.sleeps) == 1 and self.result is not None:
            rid, dispatch = self.runner.dispatched[-1]
            data = {**self.result, "run_id": dispatch["run_id"]}
            self.runner.finish(rid, RunFiles(json.dumps(data).encode()))

    def run(self, repo: str = REPO) -> int:
        return doctor.run(self.conn, self.github, self.runner, repo,
                          sleep=self.sleep, out=self.lines.append)

    @property
    def report(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def d(tmp_path):
    d = Doctor(tmp_path)
    yield d
    d.conn.close()


def test_a_healthy_enrolment_passes_every_check_and_its_smoke_run(d):
    repo = healthy(d.fake)

    code = d.run()

    assert code == 0
    assert "FAIL" not in d.report
    for check in ["App is installed", "config.yml is valid", "caller workflow",
                  "labels exist", "factory/* branches"]:
        assert check in d.report
    [(_, dispatch)] = d.runner.dispatched
    assert dispatch == {
        "contract_version": 1,
        "run_id": dispatch["run_id"],
        "repo": REPO,
        "task": "smoke",
        "base_sha": repo.head_sha,
        "enrolment_sha": repo.head_sha,
        "timeout_minutes": 20,
    }
    row = loop.get_run(d.conn, dispatch["run_id"])
    assert (row["task"], row["status"], row["outcome"]) == ("smoke", "done", "succeeded")
    assert f"smoke run {row['id']} succeeded" in d.report
    assert row["log_url"] in d.report


def test_without_the_app_installed_nothing_else_is_checked_and_no_smoke_runs(d):
    healthy(d.fake)
    d.fake.uninstall(REPO)

    assert d.run() == 1

    assert "FAIL  the App isn't installed on octo/widgets" in d.report
    assert "smoke run not dispatched" in d.report
    assert d.runner.dispatched == []


def test_an_invalid_enrolment_fails_and_no_smoke_runs(d):
    repo = healthy(d.fake)
    repo.files[".factory/config.yml"] = "setup: uv sync\n"

    assert d.run() == 1

    assert "FAIL  .factory/config.yml is invalid" in d.report
    assert "check" in d.report.split("is invalid", 1)[1].splitlines()[0]
    assert d.runner.dispatched == []


def test_a_missing_dockerfile_fails_and_no_smoke_runs(d):
    repo = healthy(d.fake)
    del repo.files[".factory/Dockerfile"]

    assert d.run() == 1

    assert "FAIL  .factory/Dockerfile isn't on the default branch" in d.report
    assert d.runner.dispatched == []


def test_a_missing_caller_workflow_fails_and_no_smoke_runs(d):
    repo = healthy(d.fake)
    del repo.files[".github/workflows/factory.yml"]

    assert d.run() == 1

    assert "FAIL  the caller workflow .github/workflows/factory.yml isn't on the default branch" in d.report
    assert d.runner.dispatched == []


def test_missing_labels_fail_but_the_smoke_run_still_goes(d):
    repo = healthy(d.fake)
    del repo.labels["agent:review"], repo.labels["needs-a-human"]

    assert d.run() == 1

    assert "FAIL  labels missing: agent:review, needs-a-human" in d.report
    assert len(d.runner.dispatched) == 1
    assert "succeeded" in d.report


def test_a_ruleset_blocking_factory_branches_fails(d):
    repo = healthy(d.fake)
    repo.rules["factory/*"] = [{"type": "required_signatures"}, {"type": "deletion"}]
    repo.rules["main"] = [{"type": "pull_request"}]

    assert d.run() == 1

    [failure] = [line for line in d.lines if line.startswith("FAIL")]
    assert "requires signed commits" in failure
    assert len(d.runner.dispatched) == 1


def test_a_private_repo_without_rulesets_on_its_plan_has_none_blocking(d):
    healthy(d.fake)
    upgrade = Response(403, {}, json.dumps({
        "message": "Upgrade to GitHub Pro or make this repository public to enable this feature.",
    }).encode())
    fake = d.fake

    def transport(request):
        return upgrade if "/rules/branches/" in request.url else fake(request)

    d.github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)

    assert d.run() == 0

    assert "ok    no ruleset" in d.report
    assert "plan" in d.report


def test_rulesets_that_cant_be_read_fail(d):
    healthy(d.fake)
    fake = d.fake

    def transport(request):
        return Response(500, {}, b"") if "/rules/branches/" in request.url else fake(request)

    d.github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)

    assert d.run() == 1

    assert "FAIL  can't read the rulesets" in d.report


def test_it_lists_the_settings_the_app_cant_read(d):
    healthy(d.fake)

    d.run()

    for setting in ["Actions is enabled", "default workflow permissions are read-only",
                    "fork PRs need approval"]:
        assert setting in d.report


def test_a_failed_smoke_run_is_reported_with_why(d):
    healthy(d.fake)
    d.result = {**example("result", "smoke.valid.json"), "outcome": "failed",
                "failure": "check_failed",
                "check": {"passed": False, "output_tail": "1 failed, 41 passed\n"}}

    assert d.run() == 1

    run_id = d.runner.dispatched[0][1]["run_id"]
    assert f"smoke run {run_id} failed" in d.report
    assert "check_failed" in d.report
    assert "1 failed, 41 passed" in d.report


def test_it_waits_until_the_smoke_run_finishes(d):
    healthy(d.fake)
    d.result = None  # the runner doesn't finish it on the first wait

    def sleep(seconds):
        d.sleeps.append(seconds)
        if len(d.sleeps) == 3:
            rid, dispatch = d.runner.dispatched[-1]
            data = {**example("result", "smoke.valid.json"), "run_id": dispatch["run_id"]}
            d.runner.finish(rid, RunFiles(json.dumps(data).encode()))

    code = doctor.run(d.conn, d.github, d.runner, REPO, sleep=sleep, out=d.lines.append)

    assert code == 0
    assert len(d.sleeps) == 3


def test_an_unavailable_runner_leaves_no_queued_smoke_run(d):
    healthy(d.fake)
    d.runner.unavailable = True

    assert d.run() == 1

    assert "couldn't be dispatched" in d.report
    active = d.conn.execute("SELECT count(*) FROM runs WHERE status != 'done'").fetchone()[0]
    assert active == 0


def test_a_smoke_run_doesnt_count_against_the_daily_cap(d):
    healthy(d.fake)

    d.run()

    assert loop.get_run(d.conn, d.runner.dispatched[0][1]["run_id"])["dispatched_at"]
    assert brakes.daily_count(d.conn, datetime.now(UTC)) == 0


def test_the_command_needs_the_app_id_and_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FACTORY_APP_ID", raising=False)
    monkeypatch.delenv("FACTORY_APP_KEY_FILE", raising=False)

    code = cli.main(["--db", str(tmp_path / "f.db"), "doctor", REPO])

    assert code == 2
    assert "FACTORY_APP_ID" in capsys.readouterr().err
