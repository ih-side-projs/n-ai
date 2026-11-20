"""SCH-1 `nai run` command line, and the WP1 hooks it relies on (terminal sinks, lazy subcommand loading)."""
import json
import os
import subprocess
import sys
import threading

import pytest
from fake_store import FakeStore
from sched_helpers import ROOT, noop

from nai import terminal
from nai.sched import JobResult, read_health
from nai.sched.cli import main


def run_cli(argv, store, registrar=None, **kw):
    return main(argv, store_factory=lambda db, bd, bk: store, llm_factory=lambda app, a: None,
                registrar=registrar or (lambda r: r.registry.register("verify", noop, uses_model=False) or ["test"]), **kw)


def test_SCH1_nai_run_drains_the_queue_and_exits_zero(tmp_path, capsys):
    s = FakeStore()
    pid = s.create_project("p", workspace_path=str(tmp_path / "ws"))
    jobs = [s.create_job(pid, "verify", "coder") for _ in range(3)]
    rc = run_cli(["--db", str(tmp_path / "x" / "nai.sqlite3"), "--drain", "--poll", "0.01"], s)
    assert rc == 0 and all(s.jobs[j]["status"] == "succeeded" for j in jobs) and s.closed
    out = capsys.readouterr().out
    assert "plugins=test" in out and "jobs_succeeded=3" in out
    assert read_health(tmp_path / "x")["state"] == "stopped"                                   # state dir defaults to the db's folder


def test_SCH1_flags_override_config_file(tmp_path, capsys):
    (tmp_path / "nai.toml").write_text('[runner]\nconcurrency = 4\nmax_consecutive_jobs = 9\nwindows = ["01:00-02:00"]\n')
    s = FakeStore()
    seen = {}
    run_cli(["--db", str(tmp_path / "n.db"), "--config", str(tmp_path / "nai.toml"), "--drain", "--concurrency", "1",
             "--window", "00:00-00:00", "--max-batch-seconds", "5"], s,
            registrar=lambda r: seen.update(c=r.sched) or [])
    assert (seen["c"].concurrency, seen["c"].max_consecutive_jobs, seen["c"].windows, seen["c"].max_batch_seconds) == (1, 9, ["00:00-00:00"], 5.0)
    assert "no job handlers are registered" in capsys.readouterr().out


@pytest.mark.parametrize("argv,msg", [(["--window", "nonsense"], "bad window"), (["--until", "whenever"], "bad --until"),
                                      (["--config", "/nope.toml"], "config file not found")])
def test_SCH1_bad_options_exit_2_with_message(argv, msg, tmp_path, capsys):
    assert run_cli(["--db", str(tmp_path / "n.db"), *argv], FakeStore()) == 2
    assert msg in capsys.readouterr().err


def test_SCH1_unknown_runner_config_key_is_rejected(tmp_path, capsys):
    (tmp_path / "nai.toml").write_text("[runner]\nbogus = 1\n")
    assert run_cli(["--db", "x", "--config", str(tmp_path / "nai.toml")], FakeStore()) == 2
    assert "unknown [runner] key: bogus" in capsys.readouterr().err


def test_STO3_second_runner_on_the_same_state_dir_is_refused(tmp_path, capsys):
    from nai.sched.health import RunnerLock
    held = RunnerLock(tmp_path)
    held.acquire()
    assert run_cli(["--db", str(tmp_path / "n.db"), "--drain"], FakeStore()) == 3
    assert "another runner holds" in capsys.readouterr().err
    held.release()


def test_SCH8_until_option_sets_stop_time(tmp_path):
    seen = {}
    run_cli(["--db", str(tmp_path / "n.db"), "--drain", "--until", "23:59"], FakeStore(), registrar=lambda r: seen.update(u=r.sched.until) or [])
    assert seen["u"].hour == 23 and seen["u"].minute == 59


def test_nai_run_without_the_store_package_explains_itself(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "nai.store", None)                                         # make `from nai.store import Store` fail
    assert main(["--db", str(tmp_path / "n.db")]) == 2
    err = capsys.readouterr().err
    assert "nai.store" in err and "WP2" in err


def test_nai_run_is_reachable_through_the_main_entry_point(tmp_path):
    r = subprocess.run([sys.executable, "-m", "nai", "run", "--help"], capture_output=True, text=True, cwd=tmp_path,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0 and "usage: nai run" in r.stdout and "--drain" in r.stdout
    r = subprocess.run([sys.executable, "-m", "nai", "--help"], capture_output=True, text=True, cwd=tmp_path,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0 and "usage: nai [-h]" in r.stdout                                  # the v1 command line is untouched


def test_nai_run_without_store_via_subprocess_exits_2(tmp_path):
    code = "import sys; sys.modules['nai.store']=None; from nai.cli import main; main(['run','--db','x.db'])"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 2 and "nai.store" in r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="signals")
def test_SCH1_sigint_once_stops_gracefully_twice_requeues(tmp_path):
    """Run `nai run` for real with a stand-in store module and send it Ctrl-C."""
    (tmp_path / "nai_fake_boot.py").write_text(f'''
import sys, time
sys.path[:0] = [{str(ROOT)!r}, {str(ROOT / "tests" / "sched")!r}]
from fake_store import FakeStore
from nai.sched.cli import main
s = FakeStore()
pid = s.create_project("p")
s.create_job(pid, "verify", "coder")
import threading
def reg(r):
    def slow(ctx):
        print("JOB-STARTED", flush=True)
        for _ in range(2000):
            ctx.check(); time.sleep(0.01)
    r.registry.register("verify", slow, uses_model=False)
    return ["t"]
rc = main(["--db", {str(tmp_path / "n.db")!r}, "--poll", "0.02"], store_factory=lambda *a: s, llm_factory=lambda a, b: None, registrar=reg)
print("JOB-STATUS", s.jobs[next(iter(s.jobs))]["status"], flush=True)
sys.exit(rc)
''')
    p = subprocess.Popen([sys.executable, str(tmp_path / "nai_fake_boot.py")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path)
    line = p.stdout.readline()
    while "JOB-STARTED" not in line:
        line = p.stdout.readline()
    p.send_signal(2)                                                                            # 1st Ctrl-C: wait for the job
    import time
    time.sleep(0.5)
    assert p.poll() is None                                                                     # still running: graceful
    p.send_signal(2)                                                                            # 2nd: cancel and requeue
    out, err = p.communicate(timeout=15)
    assert p.returncode == 0 and "JOB-STATUS queued" in out and "stopping after the running jobs finish" in out


# ------------------------------------------------------------------------------------ WP1 hooks
def test_WP1_terminal_sink_gets_lines_with_the_thread_context():
    got = []
    sink = lambda tag, text, color, ctx: got.append((tag, text, color, ctx))
    terminal.add_sink(sink)
    try:
        terminal.log("a", "outside")
        with terminal.log_context("job-1"):
            terminal.log("b", "inside", "yellow")
            with terminal.log_context("job-2"):
                terminal.log("c", "nested")
            terminal.log("d", "back")
        t = threading.Thread(target=lambda: terminal.log("e", "other thread"))
        t.start(); t.join()
    finally:
        terminal.remove_sink(sink)
    terminal.log("f", "after removal")
    assert got == [("a", "outside", "dim", None), ("b", "inside", "yellow", "job-1"), ("c", "nested", "dim", "job-2"),
                   ("d", "back", "dim", "job-1"), ("e", "other thread", "dim", None)]


def test_WP1_a_failing_sink_never_breaks_logging(capsys):
    terminal.add_sink(lambda *a: 1 / 0)
    try:
        terminal.log("x", "still printed")
    finally:
        terminal._SINKS.clear()
    assert "[x] still printed" in capsys.readouterr().out
