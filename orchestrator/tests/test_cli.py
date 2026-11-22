"""`factory runs`: the run history, read straight from SQLite."""

import json

import pytest

from factory import cli, db, loop
from factory.runner import FakeRunner, RunFiles

DISPATCH = {
    "contract_version": 1,
    "repo": "octo/widgets",
    "task": "smoke",
    "base_sha": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "enrolment_sha": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "timeout_minutes": 20,
}


@pytest.fixture
def path(tmp_path):
    return tmp_path / "factory.db"


@pytest.fixture
def conn(path):
    return db.connect(path)


def run_cli(capsys, path, *args):
    code = cli.main(["--db", str(path), *args])
    out, err = capsys.readouterr()
    return code, out, err


def test_runs_lists_the_last_20_rows_newest_first(conn, path, capsys):
    ids = [loop.enqueue(conn, repo="octo/widgets", task="smoke", dispatch=DISPATCH) for _ in range(25)]

    code, out, _ = run_cli(capsys, path, "runs")

    assert code == 0
    lines = out.splitlines()[1:]  # after the header
    assert [int(line.split()[0]) for line in lines] == ids[::-1][:20]


def test_runs_shows_each_rows_state(conn, path, capsys):
    run_id = loop.enqueue(conn, repo="octo/widgets", task="smoke", ticket="7", dispatch=DISPATCH)

    _, out, _ = run_cli(capsys, path, "runs")

    header, line = out.splitlines()
    assert header.split()[:6] == ["ID", "REPO", "TASK", "TICKET", "ATTEMPT", "STATUS"]
    assert line.split()[:6] == [str(run_id), "octo/widgets", "smoke", "7", "1", "queued"]


def test_runs_id_shows_one_row_with_its_dispatch_and_result(conn, path, capsys):
    runner = FakeRunner()
    run_id = loop.enqueue(conn, repo="octo/widgets", task="smoke", dispatch=DISPATCH)
    loop.tick(conn, runner, limit=2)
    result = {"contract_version": 1, "run_id": run_id, "task": "smoke", "outcome": "succeeded", "output": {}}
    runner.finish(loop.get_run(conn, run_id)["runner_run_id"], RunFiles(json.dumps(result).encode()))
    loop.tick(conn, runner, limit=2)

    code, out, _ = run_cli(capsys, path, "runs", str(run_id))

    assert code == 0
    assert "status: done" in out
    assert "outcome: succeeded" in out
    assert '"timeout_minutes": 20' in out  # the dispatch
    assert '"outcome": "succeeded"' in out  # the result


def test_runs_id_for_an_unknown_run_fails(conn, path, capsys):
    code, _, err = run_cli(capsys, path, "runs", "99")
    assert code == 1
    assert "no run 99" in err


def test_runs_doesnt_write_to_the_database(path, capsys):
    code, _, err = run_cli(capsys, path, "runs")
    assert code == 1
    assert not path.exists()
