"""End-to-end Go pipeline: imported spec -> stubs -> tests -> done.

Loads ``fixtures/langs/go/project.json`` and drives the real pipeline with the
scripted stub model.  Skipped unless the Go toolchain is installed; CI runs it
in the ``go`` job.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.e2e._lang_pipeline import load_fixture, run_language_pipeline

pytestmark = pytest.mark.skipif(
    shutil.which("go") is None, reason="go toolchain not installed"
)

FIXTURE = load_fixture("go")

NORMALIZER_SOURCE = (
    "package text_normalizer\n"
    "\n"
    'import "strings"\n'
    "\n"
    "func NormalizeText(value string) string {\n"
    '\treturn strings.Join(strings.Fields(value), " ")\n'
    "}\n"
)
WORD_COUNTER_SOURCE = (
    "package word_counter\n"
    "\n"
    'import "strings"\n'
    "\n"
    "func CountWords(value string) int {\n"
    "\treturn len(strings.Fields(value))\n"
    "}\n"
)
INTEGRATION_SOURCE = (
    "package root_integration\n"
    "\n"
    "import (\n"
    '\t"tiny-library/internal/text_normalizer"\n'
    '\t"tiny-library/internal/word_counter"\n'
    ")\n"
    "\n"
    "func Integrate() int {\n"
    '\treturn word_counter.CountWords(text_normalizer.NormalizeText(" a  b "))\n'
    "}\n"
)

NORMALIZER_TEST = (
    "package text_normalizer\n"
    "\n"
    'import "testing"\n'
    "\n"
    "func Test_R1_NormalizeText(t *testing.T) {\n"
    '\tif got := NormalizeText(" a  b "); got != "a b" {\n'
    '\t\tt.Fatalf("got %q", got)\n'
    "\t}\n"
    "}\n"
)
WORD_COUNTER_TEST = (
    "package word_counter\n"
    "\n"
    'import "testing"\n'
    "\n"
    "func Test_R1_CountWords(t *testing.T) {\n"
    '\tif got := CountWords("a b"); got != 2 {\n'
    '\t\tt.Fatalf("got %d", got)\n'
    "\t}\n"
    "}\n"
)
INTEGRATION_TEST = (
    "package root_integration\n"
    "\n"
    'import "testing"\n'
    "\n"
    "func Test_S1_Pipeline(t *testing.T) {\n"
    "\tif got := Integrate(); got != 2 {\n"
    '\t\tt.Fatalf("got %d", got)\n'
    "\t}\n"
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


def test_go_pipeline_imported_spec_to_done(tmp_path: Path):
    run_language_pipeline(tmp_path, language="go", fixture=FIXTURE, sources=SOURCES, tests=TESTS)
