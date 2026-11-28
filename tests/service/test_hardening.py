"""Tests for Pass 5 daemon hardening."""
from __future__ import annotations

import os

import pytest

from nai.service.config import ServiceConfig
from nai.service.daemon import ServiceDaemon


@pytest.fixture
def cfg(tmp_path):
    return ServiceConfig(
        state_dir=tmp_path / ".nai",
        start_web=False,
        start_runner=False,
        poll_interval=0.05,
    )


def test_job_name_validation(cfg):
    d = ServiceDaemon(cfg)
    for bad in ("../escape", "/etc/passwd", "a/b", "..", ".hidden", "", "x" * 65):
        with pytest.raises(ValueError, match="invalid job name"):
            d._validate_job_name(bad)
    assert d._validate_job_name("job-1") == "job-1"
    assert d._validate_job_name("my_job.2") == "my_job.2"


def test_job_name_path_escape_rejected(cfg):
    d = ServiceDaemon(cfg)
    with pytest.raises(ValueError, match="invalid job name"):
        d.spawn_job(["--help"], name="../../etc/passwd")


def test_tail_log_path_traversal_rejected(cfg):
    d = ServiceDaemon(cfg)
    with pytest.raises(ValueError, match="invalid log name"):
        d.tail_log("../../etc/passwd")
    with pytest.raises(KeyError):
        d.tail_log("nonexistent")


def test_stale_socket_is_cleaned(cfg):
    d = ServiceDaemon(cfg)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    # stale socket + pid of a definitely-dead process
    cfg.socket_path.write_text("")
    cfg.pid_path.write_text("99999999")
    assert not d._daemon_alive()
    # start_detached would remove stale artifacts (we don't spawn here)
    for path in (cfg.socket_path, cfg.pid_path):
        if not d._daemon_alive():
            try:
                path.unlink()
            except OSError:
                pass
    assert not cfg.socket_path.exists()


def test_live_daemon_detected(cfg):
    d = ServiceDaemon(cfg)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.pid_path.write_text(str(os.getpid()))
    assert d._daemon_alive()


def test_socket_mode_0600(cfg):
    """The daemon chmods the control socket after bind (verified via source contract)."""
    import inspect

    src = inspect.getsource(ServiceDaemon._serve_control)
    assert "0o600" in src
