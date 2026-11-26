"""End-to-end C pipeline: imported spec -> stubs -> tests -> done.

Loads ``fixtures/langs/c/project.json`` and drives the real pipeline with the
scripted stub model.  Skipped unless CMake and a C compiler are installed; CI
runs it in the ``c`` job.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.e2e._lang_pipeline import load_fixture, run_language_pipeline

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("cc") is None,
    reason="cmake or C compiler not installed",
)

FIXTURE = load_fixture("c")

NORMALIZER_SOURCE = (
    '#include "text_normalizer/text_normalizer.h"\n'
    "#include <stdlib.h>\n"
    "#include <string.h>\n"
    "\n"
    "char *text_normalizer_normalize_text(const char *text) {\n"
    "    if (text == NULL) {\n"
    "        return NULL;\n"
    "    }\n"
    "    size_t length = strlen(text);\n"
    "    char *out = malloc(length + 1);\n"
    "    if (out == NULL) {\n"
    "        return NULL;\n"
    "    }\n"
    "    size_t written = 0;\n"
    "    int pending_space = 0;\n"
    "    for (const char *cursor = text; *cursor != '\\0'; ++cursor) {\n"
    "        if (*cursor == ' ' || *cursor == '\\t' || *cursor == '\\n') {\n"
    "            if (written > 0) {\n"
    "                pending_space = 1;\n"
    "            }\n"
    "        } else {\n"
    "            if (pending_space) {\n"
    "                out[written++] = ' ';\n"
    "                pending_space = 0;\n"
    "            }\n"
    "            out[written++] = *cursor;\n"
    "        }\n"
    "    }\n"
    "    out[written] = '\\0';\n"
    "    return out;\n"
    "}\n"
)
WORD_COUNTER_SOURCE = (
    '#include "word_counter/word_counter.h"\n'
    "#include <ctype.h>\n"
    "#include <stddef.h>\n"
    "\n"
    "int word_counter_count_words(const char *text) {\n"
    "    if (text == NULL) {\n"
    "        return 0;\n"
    "    }\n"
    "    int count = 0;\n"
    "    int in_word = 0;\n"
    "    for (const char *cursor = text; *cursor != '\\0'; ++cursor) {\n"
    "        if (isspace((unsigned char)*cursor)) {\n"
    "            in_word = 0;\n"
    "        } else if (!in_word) {\n"
    "            in_word = 1;\n"
    "            count += 1;\n"
    "        }\n"
    "    }\n"
    "    return count;\n"
    "}\n"
)
INTEGRATION_SOURCE = (
    '#include "root_integration/root_integration.h"\n'
    '#include "text_normalizer/text_normalizer.h"\n'
    '#include "word_counter/word_counter.h"\n'
    "#include <stdlib.h>\n"
    "\n"
    "int integrate(void) {\n"
    '    char *normalized = text_normalizer_normalize_text(" a  b ");\n'
    "    if (normalized == NULL) {\n"
    "        return 0;\n"
    "    }\n"
    "    int count = word_counter_count_words(normalized);\n"
    "    free(normalized);\n"
    "    return count;\n"
    "}\n"
)

NORMALIZER_TEST = (
    '#include "nai_assert.h"\n'
    '#include "text_normalizer/text_normalizer.h"\n'
    "#include <stdlib.h>\n"
    "#include <string.h>\n"
    "\n"
    "int main(void) {\n"
    '    nai_begin("text_normalizer");\n'
    '    nai_case("R1_normalize_text");\n'
    '    char *out = text_normalizer_normalize_text(" a  b ");\n'
    '    NAI_CHECK_EQ_INT(out != NULL && strcmp(out, "a b") == 0, 1);\n'
    "    free(out);\n"
    "    return nai_finish();\n"
    "}\n"
)
WORD_COUNTER_TEST = (
    '#include "nai_assert.h"\n'
    '#include "word_counter/word_counter.h"\n'
    "\n"
    "int main(void) {\n"
    '    nai_begin("word_counter");\n'
    '    nai_case("R1_count_words");\n'
    '    NAI_CHECK_EQ_INT(word_counter_count_words("a b"), 2);\n'
    "    return nai_finish();\n"
    "}\n"
)
INTEGRATION_TEST = (
    '#include "nai_assert.h"\n'
    '#include "root_integration/root_integration.h"\n'
    "\n"
    "int main(void) {\n"
    '    nai_begin("root_integration");\n'
    '    nai_case("S1_pipeline");\n'
    "    NAI_CHECK_EQ_INT(integrate(), 2);\n"
    "    return nai_finish();\n"
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


def test_c_pipeline_imported_spec_to_done(tmp_path: Path):
    run_language_pipeline(tmp_path, language="c", fixture=FIXTURE, sources=SOURCES, tests=TESTS)
