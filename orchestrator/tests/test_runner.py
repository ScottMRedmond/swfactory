"""The fake runner behaves as the Runner interface describes."""

from factory.runner import FakeRunner, RunFiles, RunState

DISPATCH = {"run_id": 1, "task": "smoke"}


def test_a_dispatched_run_is_running_until_it_finishes():
    runner = FakeRunner()
    rid = runner.dispatch("octo/widgets", DISPATCH)
    assert runner.state("octo/widgets", rid) is RunState.RUNNING
    assert runner.files("octo/widgets", rid) is None

    files = RunFiles(result_json=b"{}", bundle=b"bundle")
    runner.finish(rid, files)
    assert runner.state("octo/widgets", rid) is RunState.FINISHED
    assert runner.files("octo/widgets", rid) == files


def test_an_unknown_run_is_missing():
    runner = FakeRunner()
    rid = runner.dispatch("octo/widgets", DISPATCH)
    assert runner.state("octo/other", rid) is RunState.MISSING
    assert runner.state("octo/widgets", "nope") is RunState.MISSING


def test_each_run_gets_its_own_id_and_log_link():
    runner = FakeRunner()
    a = runner.dispatch("octo/widgets", DISPATCH)
    b = runner.dispatch("octo/widgets", DISPATCH)
    assert a != b
    assert runner.log_link("octo/widgets", a) != runner.log_link("octo/widgets", b)


def test_cancelling_a_running_run_finishes_it_without_files():
    runner = FakeRunner()
    rid = runner.dispatch("octo/widgets", DISPATCH)
    runner.cancel("octo/widgets", rid)
    assert runner.cancelled == [rid]
    assert runner.state("octo/widgets", rid) is RunState.FINISHED
    assert runner.files("octo/widgets", rid) is None
