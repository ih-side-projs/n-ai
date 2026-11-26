"""End-to-end Rust pipeline: imported spec -> stubs -> tests -> done.

Loads ``fixtures/langs/rust/project.json`` and drives the real pipeline with the
scripted stub model.  Skipped unless the Cargo toolchain is installed; CI runs
it in the ``rust`` job.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.e2e._lang_pipeline import load_fixture, run_language_pipeline

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None, reason="cargo toolchain not installed"
)

FIXTURE = load_fixture("rust")

NORMALIZER_SOURCE = (
    "pub fn normalize_text(value: &str) -> String {\n"
    "    value.split_whitespace().collect::<Vec<_>>().join(\" \")\n"
    "}\n"
)
WORD_COUNTER_SOURCE = (
    "pub fn count_words(value: &str) -> i64 {\n"
    "    value.split_whitespace().count() as i64\n"
    "}\n"
)
INTEGRATION_SOURCE = (
    "use text_normalizer::normalize_text;\n"
    "use word_counter::count_words;\n"
    "\n"
    "pub fn integrate() -> i64 {\n"
    "    count_words(&normalize_text(\" a  b \"))\n"
    "}\n"
)

NORMALIZER_TEST = (
    "#[test]\n"
    "fn r1_normalize_text() {\n"
    "    assert_eq!(text_normalizer::normalize_text(\" a  b \"), \"a b\");\n"
    "}\n"
)
WORD_COUNTER_TEST = (
    "#[test]\n"
    "fn r1_count_words() {\n"
    "    assert_eq!(word_counter::count_words(\"a b\"), 2);\n"
    "}\n"
)
INTEGRATION_TEST = (
    "#[test]\n"
    "fn s1_pipeline() {\n"
    "    assert_eq!(root_integration::integrate(), 2);\n"
    "}\n"
)

SOURCES = {
    "text-normalizer": NORMALIZER_SOURCE,
    "word-counter": WORD_COUNTER_SOURCE,
    "root-integration": INTEGRATION_SOURCE,
}
TESTS = {
    "text-normalizer": NORMALIZER_TEST,
    "word-counter": WORD_COUNTER_TEST,
    "root-integration": INTEGRATION_TEST,
}


def test_rust_pipeline_imported_spec_to_done(tmp_path: Path):
    run_language_pipeline(tmp_path, language="rust", fixture=FIXTURE, sources=SOURCES, tests=TESTS)
