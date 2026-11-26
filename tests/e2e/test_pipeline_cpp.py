"""End-to-end C++ pipeline: imported spec -> stubs -> tests -> done.

Loads ``fixtures/langs/cpp/project.json`` and drives the real pipeline with the
scripted stub model.  Skipped unless CMake and a C++ compiler are installed; CI
runs it in the ``cpp`` job.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.e2e._lang_pipeline import load_fixture, run_language_pipeline

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("c++") is None,
    reason="cmake or C++ compiler not installed",
)

FIXTURE = load_fixture("cpp")

NORMALIZER_SOURCE = (
    '#include "text_normalizer/text_normalizer.h"\n'
    "#include <sstream>\n"
    "\n"
    "namespace text_normalizer {\n"
    "\n"
    "std::string normalize_text(const std::string &text) {\n"
    "    std::istringstream stream(text);\n"
    "    std::string word;\n"
    "    std::string out;\n"
    "    while (stream >> word) {\n"
    "        if (!out.empty()) {\n"
    "            out += \" \";\n"
    "        }\n"
    "        out += word;\n"
    "    }\n"
    "    return out;\n"
    "}\n"
    "\n"
    "}  // namespace text_normalizer\n"
)
WORD_COUNTER_SOURCE = (
    '#include "word_counter/word_counter.h"\n'
    "#include <sstream>\n"
    "\n"
    "namespace word_counter {\n"
    "\n"
    "int count_words(const std::string &text) {\n"
    "    std::istringstream stream(text);\n"
    "    std::string word;\n"
    "    int count = 0;\n"
    "    while (stream >> word) {\n"
    "        count += 1;\n"
    "    }\n"
    "    return count;\n"
    "}\n"
    "\n"
    "}  // namespace word_counter\n"
)
INTEGRATION_SOURCE = (
    '#include "root_integration/root_integration.h"\n'
    '#include "text_normalizer/text_normalizer.h"\n'
    '#include "word_counter/word_counter.h"\n'
    "\n"
    "int integrate() {\n"
    '    return word_counter::count_words(text_normalizer::normalize_text(" a  b "));\n'
    "}\n"
)

NORMALIZER_TEST = (
    '#include "doctest.h"\n'
    '#include "text_normalizer/text_normalizer.h"\n'
    "\n"
    'TEST_CASE("R1_normalize_text") {\n'
    '    CHECK(text_normalizer::normalize_text(" a  b ") == "a b");\n'
    "}\n"
)
WORD_COUNTER_TEST = (
    '#include "doctest.h"\n'
    '#include "word_counter/word_counter.h"\n'
    "\n"
    'TEST_CASE("R1_count_words") {\n'
    '    CHECK(word_counter::count_words("a b") == 2);\n'
    "}\n"
)
INTEGRATION_TEST = (
    '#include "doctest.h"\n'
    '#include "root_integration/root_integration.h"\n'
    "\n"
    'TEST_CASE("S1_pipeline") {\n'
    "    CHECK(integrate() == 2);\n"
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


def test_cpp_pipeline_imported_spec_to_done(tmp_path: Path):
    run_language_pipeline(tmp_path, language="cpp", fixture=FIXTURE, sources=SOURCES, tests=TESTS)
