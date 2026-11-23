"""Tests for TENV-1 through TENV-7 using a local wheelhouse and zero network access."""

import base64
import hashlib
import subprocess
import zipfile
from pathlib import Path

import pytest

from nai.build.tenv import (
    TargetEnv,
    TargetEnvError,
    TargetEnvManager,
    detect_stale_api,
    format_env_block,
    load_api_notes,
    parse_target_env,
)


def _build_local_wheel(wheelhouse: Path, pkg_name: str, version: str, code: str) -> Path:
    """Create a valid PEP 427 wheel completely offline in pure Python."""
    wheelhouse.mkdir(parents=True, exist_ok=True)
    mod_name = pkg_name.replace("-", "_")
    dist_info = f"{mod_name}-{version}.dist-info"
    whl_path = wheelhouse / f"{mod_name}-{version}-py3-none-any.whl"

    files = {
        f"{mod_name}/__init__.py": code.encode("utf-8"),
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {pkg_name}\nVersion: {version}\n".encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: nai-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }

    record_lines: list[str] = []
    with zipfile.ZipFile(whl_path, "w") as zf:
        for arcname, data in files.items():
            zf.writestr(arcname, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            record_lines.append(f"{arcname},sha256={digest},{len(data)}")
        record_lines.append(f"{dist_info}/RECORD,,")
        zf.writestr(f"{dist_info}/RECORD", ("\n".join(record_lines) + "\n").encode("utf-8"))

    return whl_path


def test_TENV2_offline_wheelhouse_env_and_deprecation_error(tmp_path: Path):
    wheelhouse = tmp_path / "wheelhouse"
    _build_local_wheel(
        wheelhouse,
        "dummy-lib",
        "1.2.3",
        (
            "import warnings\n"
            "def old_fn():\n"
            "    warnings.warn('old_fn is deprecated', DeprecationWarning, stacklevel=2)\n"
            "    return 1\n"
            "def new_fn():\n"
            "    return 2\n"
        ),
    )

    ws_root = tmp_path / "proj"
    ws_root.mkdir()
    project = {
        "id": "p1",
        "name": "demo",
        "settings": {
            "target_env": {
                "packages": ["dummy-lib==1.2.3"],
                "offline": True,
                "wheelhouse": str(wheelhouse),
            }
        },
    }
    tenv = parse_target_env(project, ws_root)
    mgr = TargetEnvManager()
    mgr.ensure_env(ws_root, tenv)

    # Confirm installed in venv offline and DeprecationWarning raises as error under wrapped command
    test_file = ws_root / "check_dep.py"
    test_file.write_text("import dummy_lib\ndummy_lib.old_fn()\n", encoding="utf-8")
    wrapped_fail = mgr.wrap_test_command(ws_root, tenv, f"python {test_file}")
    proc = subprocess.run(wrapped_fail, shell=True, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "DeprecationWarning" in proc.stderr

    # With allowlist entry, deprecation warning does not abort
    tenv_allow = TargetEnv(
        packages=("dummy-lib==1.2.3",),
        offline=True,
        wheelhouse=str(wheelhouse),
        deprecation_allowlist=("old_fn is deprecated",),
    )
    wrapped_ok = mgr.wrap_test_command(ws_root, tenv_allow, f"python {test_file}")
    proc_ok = subprocess.run(wrapped_ok, shell=True, capture_output=True, text=True)
    assert proc_ok.returncode == 0


def test_TENV2_offline_missing_wheel_fails(tmp_path: Path):
    empty_wh = tmp_path / "empty_wh"
    empty_wh.mkdir()
    tenv = TargetEnv(packages=("nonexistent-pkg==9.9.9",), offline=True, wheelhouse=str(empty_wh))
    with pytest.raises(TargetEnvError):
        TargetEnvManager().ensure_env(tmp_path, tenv)


def test_TENV3_lint_gate_caps_findings(tmp_path: Path):
    tenv = TargetEnv(lint_rules=("UP", "B"), lint_max_findings=2, type_checker="none")
    fake_out = "a.py:1:1: B001 bare except\na.py:2:1: UP006 use list\na.py:3:1: B007 unused"
    # Simulate ruff via run_cmd callback even if ruff binary isn't installed in container
    mgr = TargetEnvManager()
    # Patch shutil.which so ruff is detected
    import shutil

    orig_which = shutil.which
    shutil.which = lambda name: "/usr/bin/ruff" if name == "ruff" else orig_which(name)
    try:
        res = mgr.run_lint_gate(tmp_path, tenv, ["a.py"], run_cmd=lambda _c: (1, fake_out))
    finally:
        shutil.which = orig_which

    assert not res.passed
    assert len(res.findings) == 2
    assert "ruff:" in res.findings[0]


def test_TENV4_TENV5_env_block_and_api_notes_cap(tmp_path: Path):
    notes_dir = tmp_path / ".nai" / "apinotes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "httpx.md").write_text("Use httpx.Client(timeout=5.0).\n" * 50, encoding="utf-8")
    (notes_dir / "unused_pkg.md").write_text("Should not be included.", encoding="utf-8")

    tenv = TargetEnv(packages=("httpx==0.27.2", "unused-pkg==1.0.0"), api_notes_max_tokens=40)
    block = format_env_block(tenv)
    assert "httpx==0.27.2" in block
    assert "Use ONLY the standard library" in block

    notes = load_api_notes(tmp_path, tenv, "Use httpx to fetch JSON.", None)
    assert "httpx" in notes
    assert "unused_pkg" not in notes
    assert "[...truncated...]" in notes


def test_TENV6_detect_stale_api():
    tenv = TargetEnv(packages=("pydantic==2.9.2",))
    out = (
        "ImportError: cannot import name 'BaseSettings' from 'pydantic'\n"
        "AttributeError: module 'pydantic' has no attribute 'parse_obj'\n"
        "DeprecationWarning: pydantic v1 style validator\n"
    )
    hits = detect_stale_api(out, tenv)
    pkgs = {h["package"] for h in hits}
    kinds = {h["kind"] for h in hits}
    assert "pydantic" in pkgs
    assert {"ImportError", "AttributeError", "DeprecationWarning"}.issubset(kinds)
