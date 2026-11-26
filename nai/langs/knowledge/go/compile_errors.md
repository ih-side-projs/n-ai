Failure signatures

- Compile error: `go build ./...` or `go vet ./...` exits non-zero and prints
  `internal/x/x.go:12:5: undefined: Y`. The pipeline classifies these as BUILD_ERROR.
- Test failure: the build succeeds, then `go test -json` emits `{"Action":"fail",...}`
  for a test. That is TEST_FAIL.
- Stub failure: the build succeeds and the test panics with `panic: not implemented`;
  that is a valid pre-implementation failure.

Common messages and fixes

- `undefined: X`: missing declaration, wrong package, or a typo.
- `declared and not used`: remove the variable or use `_`.
- `imported and not used`: remove the import.
- `cannot use x (variable of type T) as U`: convert explicitly or change the type.
- `missing return`: every path must return a value.
- `too many arguments`/`not enough arguments`: the call does not match the signature.
- `no test files`: the test file is missing or the package path is wrong (NO_TESTS).
