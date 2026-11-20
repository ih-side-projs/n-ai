"""Tests for the nai service supervisor (thin wrapper)."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

from nai.service.cli import service_main
from nai.service.config import ServiceConfig
from nai.service.daemon import ServiceDaemon, daemon_main
from nai.service.process import ManagedProcess
from nai.service.protocol import request


class TestConfig:
    def test_defaults(self, service_config):
        assert service_config.state_dir.name == ".nai"
        assert service_config.socket_path == service_config.state_dir / "service.sock"
        assert service_config.pid_path == service_config.state_dir / "service.pid"
        assert service_config.effective_db_path == service_config.state_dir / "nai.sqlite3"

    def test_child_env(self, service_config):
        service_config.db_path = Path("/tmp/custom.sqlite3")
        env = service_config.child_env()
        assert env["NAI_DB_PATH"] == "/tmp/custom.sqlite3"
        assert "NAI_RUNNER_STATUS_FILE" in env

    def test_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NAI_WEB_PORT", "9090")
        monkeypatch.setenv("NAI_STATE_DIR", str(tmp_path / "state"))
        cfg = ServiceConfig.from_env()
        assert cfg.web_port == 9090
        assert cfg.state_dir == tmp_path / "state"


class TestManagedProcess:
    def test_start_stop_and_log(self, service_config):
        proc = ManagedProcess(
            name="fake",
            command=[sys.executable, "-u", "-c",
                     "import time; print('hello', flush=True); time.sleep(30)"],
            cwd=Path.cwd(),
            env=dict(os.environ),
            log_path=service_config.log_dir / "fake.log",
            auto_restart=False,
        )
        proc.start()
        try:
            assert proc.running
            assert proc.pid
            for _ in range(40):
                if "hello" in proc.tail_log():
                    break
                time.sleep(0.05)
            assert "hello" in proc.tail_log()
            assert proc.status()["state"] == "running"
        finally:
            proc.stop()
        assert not proc.running
        assert proc.status()["state"] == "stopped"

    def test_fast_exit_is_stopped(self, service_config):
        proc = ManagedProcess(
            name="fast",
            command=[sys.executable, "-c", "pass"],
            cwd=Path.cwd(),
            env=dict(os.environ),
            log_path=service_config.log_dir / "fast.log",
        )
        proc.start()
        time.sleep(0.3)
        assert proc.poll() == "stopped"


class TestDaemon:
    def test_status_and_dispatch(self, service_config):
        daemon = ServiceDaemon(service_config)
        status = daemon.status()
        assert status["pid"] == os.getpid()
        assert status["children"] == {}
        assert status["jobs"] == {}
        assert "web_url" in status

    def test_spawn_job_and_jobs(self, service_config):
        daemon = ServiceDaemon(service_config)
        data = daemon.dispatch("exec", {"argv": ["--help"], "name": "help-job"})
        assert data["id"] == "help-job"
        assert data["pid"]
        jobs = daemon.dispatch("jobs", {})
        assert "help-job" in jobs["jobs"]
        job = daemon.dispatch("job", {"id": "help-job"})
        assert job["name"] == "help-job"
        daemon.dispatch("cancel", {"id": "help-job"})

    def test_unknown_commands_raise(self, service_config):
        daemon = ServiceDaemon(service_config)
        with pytest.raises(ValueError):
            daemon.dispatch("nonsense", {})
        with pytest.raises(KeyError):
            daemon.dispatch("job", {"id": "missing"})

    def test_control_socket_round_trip(self, service_config):
        daemon = ServiceDaemon(service_config)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(service_config.socket_path))
        server.listen(4)
        server.settimeout(0.5)

        import threading

        def serve():
            try:
                conn, _ = server.accept()
                with conn:
                    from nai.service.protocol import read_request, write_response

                    req = read_request(conn)
                    write_response(conn, ok=True, data={"echo": req["cmd"], **req["args"]})
            finally:
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        response = request(service_config.socket_path, "ping", a=1)
        thread.join(timeout=2)
        assert response["ok"] is True
        assert response["data"]["echo"] == "ping"
        assert response["data"]["a"] == 1

    def test_shell_help(self, service_config, capsys, monkeypatch):
        from nai.service.cli import _shell

        inputs = iter(["help", "exit"])
        monkeypatch.setattr("builtins.input", lambda _="": next(inputs))
        assert _shell(service_config) == 0
        out = capsys.readouterr().out
        assert "service shell" in out
        assert "submit" in out


class TestCLI:
    def test_status_when_stopped(self, service_config, capsys):
        rc = service_main(["--state-dir", str(service_config.state_dir), "status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["running"] is False

    def test_exec_without_service_fails(self, service_config, capsys):
        rc = service_main(["--state-dir", str(service_config.state_dir), "exec", "--", "--help"])
        assert rc == 1
        assert "not running" in capsys.readouterr().err
