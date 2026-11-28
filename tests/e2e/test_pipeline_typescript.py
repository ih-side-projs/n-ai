"""End-to-end TypeScript pipeline: imported spec -> stubs -> tests -> done.

Loads ``fixtures/langs/typescript/project.json`` and drives the real pipeline
with the scripted stub model.  Skipped unless Node (>= 20) and npm are present;
CI runs it in the ``typescript`` job.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.e2e._lang_pipeline import load_fixture, run_language_pipeline

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("npm") is None,
    reason="node/npm toolchain not installed",
)

FIXTURE = load_fixture("typescript")

NORMALIZER_SOURCE = (
    "export function normalizeText(value: string): string {\n"
    "  return value.trim().split(/\\s+/).filter(Boolean).join(\" \");\n"
    "}\n"
)
WORD_COUNTER_SOURCE = (
    "export function countWords(value: string): number {\n"
    "  return value.trim().split(/\\s+/).filter(Boolean).length;\n"
    "}\n"
)
INTEGRATION_SOURCE = (
    "import { normalizeText } from \"../text-normalizer/index.js\";\n"
    "import { countWords } from \"../word-counter/index.js\";\n"
    "\n"
    "export function integrate(): number {\n"
    "  return countWords(normalizeText(\" a  b \"));\n"
    "}\n"
)

NORMALIZER_TEST = (
    "import { describe, expect, it } from \"vitest\";\n"
    "import { normalizeText } from \"../src/text-normalizer/index.js\";\n"
    "\n"
    "describe(\"normalizeText\", () => {\n"
    "  it(\"R1_collapses whitespace\", () => {\n"
    "    expect(normalizeText(\" a  b \")).toBe(\"a b\");\n"
    "  });\n"
    "});\n"
)
WORD_COUNTER_TEST = (
    "import { describe, expect, it } from \"vitest\";\n"
    "import { countWords } from \"../src/word-counter/index.js\";\n"
    "\n"
    "describe(\"countWords\", () => {\n"
    "  it(\"R1_counts whitespace separated words\", () => {\n"
    "    expect(countWords(\"a b\")).toBe(2);\n"
    "  });\n"
    "});\n"
)
INTEGRATION_TEST = (
    "import { describe, expect, it } from \"vitest\";\n"
    "import { integrate } from \"../src/root-integration/index.js\";\n"
    "\n"
    "describe(\"integrate\", () => {\n"
    "  it(\"S1_wires the components\", () => {\n"
    "    expect(integrate()).toBe(2);\n"
    "  });\n"
    "});\n"
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


def test_typescript_pipeline_imported_spec_to_done(tmp_path: Path):
    run_language_pipeline(tmp_path, language="typescript", fixture=FIXTURE, sources=SOURCES, tests=TESTS)
