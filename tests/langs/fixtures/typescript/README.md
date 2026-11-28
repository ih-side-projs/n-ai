# Captured TypeScript/vitest output samples

These files are **hand-captured samples** of the output the TypeScript adapter
parses. They pin the formats the adapter relies on, so the parser tests keep
working on a machine without Node.

| File | Command | Exit | What it pins |
|------|---------|------|--------------|
| `tsc_build_error.txt` | `npx tsc --noEmit` | 2 | `path(line,col): error TSxxxx: message` diagnostics |
| `vitest_pass.json` | `npx vitest run --reporter=json` | 0 | `testResults[].assertionResults[]` with `status: passed` |
| `vitest_fail.json` | the same on a failing spec | 1 | `status: failed` plus `failureMessages` |
| `vitest_stub_crash.json` | the same against a `throw new Error("not implemented")` stub | 1 | the stub marker inside `failureMessages` |
| `zero_tests.json` | the same with no spec files | 0 | `numTotalTests: 0` |

The JSON reporter is written to stdout, so `_extract_json` scans for the first
complete object and ignores the ANSI-free banner text vitest prints around it.
Re-capture with Node 20 / vitest 2 when the toolchain changes.
