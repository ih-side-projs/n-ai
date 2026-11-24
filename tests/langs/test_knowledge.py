"""Knowledge packs: completeness, the ~600-token cap, and injection by job kind."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nai.build.context_pack import assemble_context_pack
from nai.build.tenv import TargetEnv
from nai.langs import knowledge, registry
from tests.build.conftest import FakeStore


@pytest.mark.parametrize("language_id", registry.supported())
def test_every_registered_language_ships_a_complete_pack(language_id: str):
    assert knowledge.has_pack(language_id), knowledge.missing_sections(language_id)
    for section in knowledge.SECTIONS:
        text = knowledge.load_section(language_id, section)
        assert text.strip(), f"{language_id}/{section} is empty"


@pytest.mark.parametrize("language_id", registry.supported())
@pytest.mark.parametrize("section", knowledge.SECTIONS)
def test_sections_stay_under_the_token_cap(language_id: str, section: str):
    text = knowledge.load_section(language_id, section)
    assert len(text) <= knowledge.MAX_SECTION_CHARS
    assert knowledge.estimate_tokens(text) <= knowledge.MAX_SECTION_TOKENS


def test_python_pack_states_tool_versions():
    spec = knowledge.load_section("python", "spec")
    impl = knowledge.load_section("python", "impl")
    tests = knowledge.load_section("python", "test")
    assert "3.12" in spec
    assert "3.12" in impl
    assert "pytest 8" in tests


def test_job_kind_mapping_covers_the_build_stages():
    assert knowledge.sections_for_job("implement") == ("impl", "stub", "pitfalls")
    assert knowledge.sections_for_job("author_tests") == ("test", "pitfalls")
    assert knowledge.sections_for_job("contract_stub") == ("stub", "compile_errors")
    assert knowledge.sections_for_job("verify") == ("compile_errors",)
    assert knowledge.sections_for_job("not-a-job") == ()
    assert knowledge.sections_for_job(None) == ()


def test_block_renders_only_the_sections_for_the_job():
    impl_block = knowledge.block("python", "implement")
    assert "Language Knowledge: python" in impl_block
    assert "Implementation" in impl_block
    assert "Compile / import errors" not in impl_block

    assert knowledge.block("python", "unknown-job") == ""
    assert knowledge.load_section("python", "test").strip() in knowledge.block("python", "author_tests")


def test_load_section_rejects_unknown_sections():
    with pytest.raises(KeyError):
        knowledge.load_section("python", "nope")


def _pack(tmp_path: Path, job_kind: str | None):
    store = FakeStore(tmp_path)
    pid = store.create_project("demo", workspace_path=str(tmp_path))
    node_id = store.create_node(pid, "api", "leaf", objective="API layer", spec_text="- R1: Do it.")
    return assemble_context_pack(
        store,
        store.get_project(pid),
        store.get_node(node_id),
        TargetEnv(),
        coder_ctx_limit=32768,
        workspace_root=tmp_path,
        job_kind=job_kind,
    )


def test_context_pack_injects_knowledge_for_the_job_kind(tmp_path: Path):
    pack = _pack(tmp_path, "implement")
    assert "language_knowledge" in pack.sections
    assert "Language Knowledge: python" in pack.text
    assert "### Implementation" in pack.text
    assert pack.fits


def test_context_pack_omits_knowledge_without_a_job_kind(tmp_path: Path):
    pack = _pack(tmp_path, None)
    assert pack.sections["language_knowledge"] == ""
    assert "Language Knowledge" not in pack.text


def test_context_pack_style_guide_comes_from_the_adapter(tmp_path: Path):
    from nai.langs import registry

    assert "PEP 8" in _pack(tmp_path, "implement").text

    adapter = SimpleNamespace(
        id="stubish",
        display_name="Stubish",
        test_framework="stubtest",
        stack_hint="Stubish 1.0",
        style_guide="- Keep it boring.",
        toolchain=(),
    )
    registry.register(adapter)  # type: ignore[arg-type]
    try:
        store = FakeStore(tmp_path)
        pid = store.create_project("demo", workspace_path=str(tmp_path), settings={"language": "stubish"})
        node_id = store.create_node(pid, "api", "leaf", objective="API")
        pack = assemble_context_pack(
            store, store.get_project(pid), store.get_node(node_id), TargetEnv(),
            coder_ctx_limit=32768, workspace_root=tmp_path, job_kind="implement",
        )
    finally:
        registry.unregister("stubish")

    assert "Keep it boring." in pack.text
    assert "PEP 8" not in pack.text
