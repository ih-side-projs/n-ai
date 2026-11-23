"""Tests for BLD-1 deterministic context pack assembly and token budget flagging."""

from pathlib import Path

from nai.build.context_pack import assemble_context_pack
from nai.build.tenv import TargetEnv
from tests.build.conftest import FakeStore


def test_BLD1_context_pack_includes_dep_signatures_only_and_enforces_cap(tmp_path: Path):
    store = FakeStore(tmp_path)
    pid = store.create_project("demo", workspace_path=str(tmp_path))
    dep_id = store.create_node(pid, "auth", "leaf", objective="Auth helper")
    node_id = store.create_node(
        pid,
        "api",
        "leaf",
        objective="API layer",
        spec_text="- R1: Calls verify_token.",
        depends_on=(dep_id,),
    )

    dep_file = tmp_path / "src" / "demo" / "auth.py"
    dep_file.parent.mkdir(parents=True, exist_ok=True)
    dep_file.write_text(
        'def verify_token(tok: str) -> bool:\n    """Verify token."""\n    SECRET_KEY = "never_leak_impl"\n    return tok == SECRET_KEY\n',
        encoding="utf-8",
    )

    project = store.get_project(pid)
    node = store.get_node(node_id)
    tenv = TargetEnv(packages=())

    pack = assemble_context_pack(store, project, node, tenv, coder_ctx_limit=32768, workspace_root=tmp_path)
    assert pack.fits
    assert "def verify_token(tok: str) -> bool:" in pack.text
    assert "never_leak_impl" not in pack.text
    assert "Target Environment (TENV-4)" in pack.text

    # Tiny coder_ctx triggers fits=False for decomposition flag
    too_small = assemble_context_pack(store, project, node, tenv, coder_ctx_limit=40, workspace_root=tmp_path)
    assert not too_small.fits
