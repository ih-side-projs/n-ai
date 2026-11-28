# Captured Go toolchain output samples

**Hand-captured samples** of `go test -json`, `go build` and `go vet` output,
kept so the parser tests run without a Go toolchain.

| File | Command | Exit | What it pins |
|------|---------|------|--------------|
| `go_test_pass.txt` | `go test -json ./...` | 0 | `Action: pass` per test plus the package-level pass |
| `go_test_fail.txt` | the same on a failing test | 1 | `Action: fail` and the `--- FAIL` detail line |
| `go_test_stub_crash.txt` | the same against a `panic("not implemented")` stub | 1 | the panic output attached to the failing test |
| `go_build_error.txt` | `go build ./...` | 1 | `# package` then `file.go:line:col: message` |
| `go_vet_error.txt` | `go vet ./...` | 1 | vet's `file.go:line:col: message` lines |
| `zero_tests.txt` | `go test -json ./...` with no test files | 0 | package events only, no `Test` field |

Every line is a real JSON object (`go test -json` emits one per event), so the
adapter never has to parse Go's human-readable summary. Re-capture with Go 1.22.
