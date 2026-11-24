"""Reading Claude usage with `claude -p "/usage" --output-format stream-json
--verbose` (orchestrator.md#reading-claude-usage). A fake `claude` on PATH
prints what the real one does."""

import json
import os
import stat

import pytest

from factory import usage

LIMITS = [
    {"kind": "session", "group": "session", "percent": 25,
     "resets_at": "2026-11-23T00:40:00.179957+00:00", "scope": None,
     "severity": "normal", "is_active": True},
    {"kind": "weekly_all", "group": "weekly", "percent": 4,
     "resets_at": "2026-11-24T18:00:00.179977+00:00", "scope": None,
     "severity": "normal", "is_active": False},
]


def lines(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


INIT = {"type": "system", "subtype": "init"}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0}


def full_login(limits=LIMITS):
    report = {"session": {"total_cost_usd": 0},
              "rate_limits": None if limits is None else {"limits": limits}}
    return lines(INIT, {"type": "assistant", "message": {"content": []}, "usage_report": report}, RESULT)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Put a `claude` on PATH that records its arguments and prints `stdout`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def install(stdout: str, exit_code: int = 0):
        (tmp_path / "out").write_text(stdout)
        script = bin_dir / "claude"
        script.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/args\ncat {tmp_path}/out\nexit {exit_code}\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return tmp_path / "args"

    return install


def test_a_full_login_gives_the_limit_rows(fake_claude):
    args = fake_claude(full_login())

    assert usage.read_claude() == (LIMITS, None)
    assert args.read_text().split() == ["-p", "/usage", "--output-format", "stream-json", "--verbose"]


def test_a_setup_token_login_has_no_usage_report(fake_claude):
    fake_claude(lines(INIT, {"type": "assistant", "message": {"content": []}}, RESULT))

    assert usage.read_claude() == (None, "no usage_report")


def test_null_rate_limits_is_unreadable(fake_claude):
    fake_claude(full_login(limits=None))

    assert usage.read_claude() == (None, "rate_limits is null")


def test_a_changed_format_is_unreadable(fake_claude):
    fake_claude(full_login(limits=[{"kind": "session", "used": "a lot"}]))

    limits, error = usage.read_claude()
    assert limits is None and error == "unexpected usage_report format"


def test_a_failing_claude_is_unreadable(fake_claude):
    fake_claude("not json\n", exit_code=1)

    assert usage.read_claude() == (None, "claude exited 1")


def test_no_claude_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))

    limits, error = usage.read_claude()
    assert limits is None and error.startswith("can't run claude")
