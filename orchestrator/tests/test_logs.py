"""The orchestrator's log: JSON lines, rotated daily, 30 days kept, tokens redacted."""

import json
import logging
import threading

import pytest

from factory import logs


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "logs"
    handler = logs.setup(d)
    yield d
    logging.getLogger("factory").removeHandler(handler)
    handler.close()


def lines(log_dir):
    return [json.loads(line) for line in (log_dir / logs.LOG_NAME).read_text().splitlines()]


def test_a_line_about_a_run_carries_run_repo_and_ticket(log_dir):
    logging.getLogger("factory.loop").warning(
        "run %s is missing", 12, extra=logs.about_run(id=12, repo="octo/widgets", ticket="7")
    )

    [line] = lines(log_dir)
    assert line["level"] == "warning"
    assert line["msg"] == "run 12 is missing"
    assert (line["run"], line["repo"], line["ticket"]) == (12, "octo/widgets", "7")
    assert line["time"].endswith("+00:00")


def test_tokens_are_redacted_from_every_field(log_dir):
    token = "ghs_16C7e42F292c6912E7710c838347Ae178B4a"
    log = logging.getLogger("factory.github")
    log.info("got token %s", token, extra={"repo": f"octo/{token}"})
    try:
        raise RuntimeError(f"bad sk-ant-oat01-secretsecret")
    except RuntimeError:
        log.exception("boom")

    text = (log_dir / logs.LOG_NAME).read_text()
    assert token not in text
    assert "sk-ant-oat01" not in text
    assert lines(log_dir)[0]["msg"] == "got token [REDACTED]"
    assert "RuntimeError" in lines(log_dir)[1]["exc"]


def test_rotation_keeps_30_days(log_dir):
    for day in range(1, 36):
        (log_dir / f"{logs.LOG_NAME}.2026-10-{day:02d}").write_text("{}\n")
    handler = next(h for h in logging.getLogger("factory").handlers if isinstance(h, logs.Handler))

    handler.doRollover()

    rotated = sorted(p.name for p in log_dir.iterdir() if p.name != logs.LOG_NAME)
    assert len(rotated) == 30


def test_records_read_back_oldest_first_across_rotated_files(tmp_path):
    (tmp_path / f"{logs.LOG_NAME}.2026-11-20").write_text(json.dumps({"msg": "a"}) + "\n")
    (tmp_path / f"{logs.LOG_NAME}.2026-11-21").write_text(json.dumps({"msg": "b"}) + "\nnot json\n")
    (tmp_path / logs.LOG_NAME).write_text(json.dumps({"msg": "c"}) + "\n")

    assert [r["msg"] for r in logs.read(tmp_path)] == ["a", "b", "not json", "c"]


def test_follow_yields_new_records_as_they_are_written(tmp_path):
    path = tmp_path / logs.LOG_NAME
    path.write_text(json.dumps({"msg": "old"}) + "\n")
    stop = threading.Event()
    seen = []

    def reader():
        for record in logs.follow(tmp_path, interval=0.01, stop=stop):
            seen.append(record["msg"])
            if len(seen) == 2:
                stop.set()

    thread = threading.Thread(target=reader)
    thread.start()
    with path.open("a") as f:
        f.write(json.dumps({"msg": "new"}) + "\n")
    thread.join(timeout=2)

    assert seen == ["old", "new"]


def test_the_loops_lines_about_a_run_carry_its_run_repo_and_ticket(log_dir, tmp_path):
    from factory import db, loop
    from factory.runner import FakeRunner

    conn = db.connect(tmp_path / "factory.db")
    runner = FakeRunner()
    dispatch = {
        "contract_version": 1,
        "repo": "octo/widgets",
        "task": "smoke",
        "base_sha": "0" * 40,
        "enrolment_sha": "0" * 40,
        "timeout_minutes": 20,
    }
    run_id = loop.enqueue(conn, repo="octo/widgets", task="smoke", ticket="7", dispatch=dispatch)
    loop.tick(conn, runner, limit=2)
    runner.forget(loop.get_run(conn, run_id)["runner_run_id"])
    loop.tick(conn, runner, limit=2)

    [line] = [entry for entry in lines(log_dir) if entry["level"] == "warning"]
    assert (line["run"], line["repo"], line["ticket"]) == (run_id, "octo/widgets", "7")


def test_format_makes_a_readable_line():
    record = {
        "time": "2026-11-22T19:30:50+00:00",
        "level": "warning",
        "logger": "factory.loop",
        "msg": "run 12 is missing",
        "run": 12,
        "repo": "octo/widgets",
        "ticket": "7",
    }

    assert logs.format(record) == (
        "2026-11-22 19:30:50  WARNING  run 12 octo/widgets#7  run 12 is missing"
    )
