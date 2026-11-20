"""Fixtures for service tests — everything is local and uses short-lived fake processes."""
import sys
from pathlib import Path

import pytest

from nai.service.config import ServiceConfig


@pytest.fixture
def service_config(tmp_path: Path) -> ServiceConfig:
    return ServiceConfig(
        state_dir=tmp_path / ".nai",
        python=sys.executable,
        start_web=False,
        start_runner=False,
        auto_restart=False,
        poll_interval=0.05,
        stop_timeout=2.0,
    )
