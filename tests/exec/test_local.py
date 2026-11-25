"""LocalTarget: scrubbed environment, limits, network phases, streaming, cancellation."""

from __future__ import annotations

import sys
import threading
import time

from nai.exec import CANCEL_EXIT, PHASE_RUN, PHASE_SETUP, TIMEOUT_EXIT
from nai.exec.local import LocalTarget


def _plain(tmp_path, **kwargs) -> LocalTarget:
    """A local target with no namespace/limits, so basic runs stay fast."""
    target = LocalTarget(tmp_path, isolate_network=False, **kwargs)
    target._prlimit = None
    target._bash = None
    return target


# --- running -----------------------------------------------------------------


def test_runs_argv_and_returns_output(tmp_path):
    target = _plain(tmp_path)
    result = target.run([sys.executable, "-c", "print('hi')"], phase=PHASE_SETUP)
    assert result.exit_code == 0
    assert result.ok
    assert "hi" in result.output
    assert result.mode == "local"
    assert result.argv == (sys.executable, "-c", "print('hi')")


def test_nonzero_exit_is_returned_not_raised(tmp_path):
    target = _plain(tmp_path)
    result = target.run([sys.executable, "-c", "import sys; sys.exit(3)"], phase=PHASE_SETUP)
    assert result.exit_code == 3
    assert not result.ok


def test_empty_argv_is_rejected(tmp_path):
    target = _plain(tmp_path)
    result = target.run([])
    assert result.exit_code == 2
    assert result.error == "empty argv"


def test_cwd_is_honoured(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    target = _plain(tmp_path)
    result = target.run(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=str(sub),
        phase=PHASE_SETUP,
    )
    assert str(sub.resolve()) in result.output


def test_output_is_capped_with_head_and_tail(tmp_path):
    target = _plain(tmp_path)
    result = target.run(
        [sys.executable, "-c", "print('x' * 20000)"],
        output_cap=200,
        phase=PHASE_SETUP,
    )
    assert result.truncated
    assert "chars truncated" in result.output
    assert len(result.output) < 1000


def test_output_is_streamed_to_the_sink(tmp_path):
    seen: list[str] = []
    target = _plain(tmp_path, sink=seen.append)
    result = target.run([sys.executable, "-c", "print('streamed')"], phase=PHASE_SETUP)
    assert result.exit_code == 0
    assert "streamed" in "".join(seen)


# --- environment scrubbing ---------------------------------------------------


def test_child_environment_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("LLAMA_SWAP_KEY", "super-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    target = _plain(tmp_path)
    result = target.run(
        [
            sys.executable,
            "-c",
            "import os; print('PATH' in os.environ); print(os.environ.get('LLAMA_SWAP_KEY', 'MISSING'))",
        ],
        phase=PHASE_SETUP,
    )
    assert result.exit_code == 0
    assert "True" in result.output
    assert "MISSING" in result.output
    assert "super-secret" not in result.output


def test_env_override_secret_is_dropped(tmp_path):
    target = _plain(tmp_path)
    result = target.run(
        [sys.executable, "-c", "import os; print(os.environ.get('LLAMA_SWAP_KEY', 'MISSING'))"],
        env_overrides={"LLAMA_SWAP_KEY": "leak"},
        phase=PHASE_SETUP,
    )
    assert "MISSING" in result.output
    assert "leak" not in result.output


def test_adapter_env_overrides_reach_the_child(tmp_path):
    target = _plain(tmp_path)
    result = target.run(
        [sys.executable, "-c", "import os; print(os.environ.get('PYTHONPATH', 'MISSING'))"],
        env_overrides={"PYTHONPATH": "src"},
        phase=PHASE_SETUP,
    )
    assert "src" in result.output


# --- timeouts and cancellation ----------------------------------------------


def test_timeout_kills_and_marks(tmp_path):
    target = _plain(tmp_path)
    started = time.monotonic()
    result = target.run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=1,
        phase=PHASE_SETUP,
    )
    assert result.timed_out
    assert result.exit_code == TIMEOUT_EXIT
    assert "timed out" in result.output
    assert time.monotonic() - started < 6


def test_cancel_kills_the_process_group(tmp_path):
    target = _plain(tmp_path)
    results: list = []

    def _run() -> None:
        results.append(
            target.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=30)
        )

    thread = threading.Thread(target=_run)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and target._proc is None:
        time.sleep(0.02)
    assert target.cancel() is True
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert results and results[0].cancelled
    assert results[0].exit_code == CANCEL_EXIT
    assert "cancelled" in results[0].output
    assert target.cancel() is False          # nothing is running any more


def test_cancelled_run_does_not_poison_the_next_run(tmp_path):
    target = _plain(tmp_path)
    results: list = []

    def _run():
        results.append(
            target.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=30)
        )

    thread = threading.Thread(target=_run)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and target._proc is None:
        time.sleep(0.02)
    assert target.cancel() is True
    thread.join(timeout=10)
    assert results and results[0].cancelled
    after = target.run([sys.executable, "-c", "print('ok')"], phase=PHASE_SETUP)
    assert after.exit_code == 0
    assert not after.cancelled
    assert "ok" in after.output


def test_cancel_event_stops_the_run(tmp_path):
    target = _plain(tmp_path)
    event = threading.Event()
    results: list = []

    def _run() -> None:
        results.append(
            target.run(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=30,
                cancel_event=event,
            )
        )

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.3)
    event.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert results and results[0].cancelled


# --- network phases ----------------------------------------------------------


def test_network_prefix_uses_unshare_when_available(tmp_path):
    target = _plain(tmp_path)
    target._unshare = "/usr/bin/unshare"
    argv, weaker = target._network_prefix(["echo", "hi"], False)
    assert argv == ["/usr/bin/unshare", "-rn", "--", "echo", "hi"]
    assert weaker is False


def test_network_prefix_allows_network_in_setup(tmp_path):
    target = _plain(tmp_path)
    argv, weaker = target._network_prefix(["echo"], True)
    assert argv == ["echo"]
    assert weaker is False


def test_weaker_isolation_warns_once_and_records(tmp_path):
    notes: list = []
    target = _plain(tmp_path, on_note=lambda *args: notes.append(args))
    target._unshare = None
    first = target.run([sys.executable, "-c", "print(1)"], phase=PHASE_RUN)
    assert first.weaker_isolation is True
    assert first.network == "off"
    assert len(notes) == 1
    assert notes[0][0] == "exec.weaker_isolation"
    second = target.run([sys.executable, "-c", "print(2)"], phase=PHASE_RUN)
    assert second.weaker_isolation is True
    assert len(notes) == 1                    # warned once per target
    assert "weaker mode" in " ".join(target.notes)


def test_setup_phase_does_not_warn(tmp_path):
    notes: list = []
    target = _plain(tmp_path, on_note=lambda *args: notes.append(args))
    target._unshare = None
    result = target.run([sys.executable, "-c", "print(1)"], phase=PHASE_SETUP)
    assert result.network == "on"
    assert result.weaker_isolation is False
    assert notes == []


# --- prefixing ---------------------------------------------------------------


def test_limit_prefix_uses_prlimit(tmp_path):
    target = _plain(tmp_path)
    target._prlimit = "/usr/bin/prlimit"
    target._bash = None
    prefix = target._limit_prefix(["echo", "hi"], 60, True)
    assert prefix[0] == "/usr/bin/prlimit"
    assert "--as=4294967296" in prefix
    assert "--cpu=90" in prefix
    assert "--nproc=512" in prefix
    assert prefix[-3:] == ["--", "echo", "hi"]


def test_limit_prefix_uses_ulimit_fallback(tmp_path):
    target = _plain(tmp_path)
    target._prlimit = None
    target._bash = "/bin/bash"
    prefix = target._limit_prefix(["echo", "hi"], 60, True)
    assert prefix[0] == "/bin/bash"
    assert prefix[1] == "-c"
    assert "ulimit -v" in prefix[2]
    assert prefix[3] == "nai-exec"
    assert prefix[-2:] == ["echo", "hi"]


def test_limit_prefix_is_a_noop_without_limits(tmp_path):
    target = _plain(tmp_path)
    assert target._limit_prefix(["echo"], 60, False) == ["echo"]


# --- capabilities and describe ----------------------------------------------


def test_capabilities_and_describe(tmp_path):
    target = _plain(tmp_path)
    caps = target.capabilities()
    assert caps.mode == "local"
    assert caps.streaming is True
    assert caps.real_cancel is True
    assert "local process group" in target.describe()
    assert "network" in target.describe()


def test_describe_reports_weaker_mode(tmp_path):
    target = _plain(tmp_path)
    target._unshare = None
    assert "unavailable" in target.describe()


def test_describe_reports_ulimit_fallback(tmp_path):
    target = _plain(tmp_path)
    target._prlimit = None
    target._bash = "/bin/bash"
    assert "ulimit" in target.describe()


def test_describe_reports_no_limits(tmp_path):
    target = _plain(tmp_path)
    target._prlimit = None
    target._bash = None
    assert "no local limits" in target.describe()
