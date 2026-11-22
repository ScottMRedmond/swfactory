"""`factory logs`: the orchestrator's log as readable lines."""

import json

from factory import cli


def write_log(log_dir, *records):
    log_dir.mkdir(exist_ok=True)
    (log_dir / "factory.log").write_text("".join(json.dumps(r) + "\n" for r in records))


RECORDS = [
    {"time": "2026-11-22T10:00:00+00:00", "level": "info", "logger": "factory", "msg": "polled"},
    {
        "time": "2026-11-22T10:01:00+00:00",
        "level": "warning",
        "logger": "factory.loop",
        "msg": "run 12 is missing",
        "run": 12,
        "repo": "octo/widgets",
        "ticket": "7",
    },
    {
        "time": "2026-11-22T10:02:00+00:00",
        "level": "info",
        "logger": "factory.loop",
        "msg": "run 13 dispatched",
        "run": 13,
        "repo": "octo/gadgets",
        "ticket": "3",
    },
    {
        "time": "2026-11-22T10:03:00+00:00",
        "level": "error",
        "logger": "factory",
        "msg": "poll failed",
        "repo": "octo/gadgets",
    },
]


def run_logs(capsys, tmp_path, *args):
    argv = ["--db", str(tmp_path / "none.db"), "--logs", str(tmp_path / "logs"), "logs", *args]
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def messages(out):
    return [line.split("  ")[-1] for line in out]


def test_logs_shows_every_line_readably(tmp_path, capsys):
    write_log(tmp_path / "logs", *RECORDS)

    code, out, _ = run_logs(capsys, tmp_path)

    assert code == 0
    assert out[1] == "2026-11-22 10:01:00  WARNING  run 12 octo/widgets#7  run 12 is missing"
    assert len(out) == 4


def test_logs_filters_by_run(tmp_path, capsys):
    write_log(tmp_path / "logs", *RECORDS)
    _, out, _ = run_logs(capsys, tmp_path, "--run", "13")
    assert messages(out) == ["run 13 dispatched"]


def test_logs_filters_by_repo(tmp_path, capsys):
    write_log(tmp_path / "logs", *RECORDS)
    _, out, _ = run_logs(capsys, tmp_path, "--repo", "octo/gadgets")
    assert messages(out) == ["run 13 dispatched", "poll failed"]


def test_logs_filters_by_level_and_above(tmp_path, capsys):
    write_log(tmp_path / "logs", *RECORDS)
    _, out, _ = run_logs(capsys, tmp_path, "--level", "warn")
    assert messages(out) == ["run 12 is missing", "poll failed"]


def test_logs_with_no_log_yet_says_so(tmp_path, capsys):
    code, out, err = run_logs(capsys, tmp_path)
    assert code == 0
    assert out == []
    assert "no log" in err
