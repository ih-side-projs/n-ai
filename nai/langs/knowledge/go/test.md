`go test -json ./...`, standard `testing` package only.

Shape

```go
package word_counter

import "testing"

func Test_R1_CountWords(t *testing.T) {
	got := CountWords(" a  b ")
	if got != 2 {
		t.Fatalf("CountWords: got %d, want 2", got)
	}
}
```

Rules

- One file per component: `internal/<component>/<component>_test.go`, same package.
- Name each test `Test_<REQ-ID>_<Behaviour>`; traceability needs the id between
  underscores (`Test_R1_CountWords`), because `TestR1_...` has no separator
  before the id.
- Use `t.Fatalf`/`t.Errorf` with the requirement id in the message.
- Table-driven tests: `for _, tc := range []struct{...}{...} { t.Run(tc.name, ...) }`.
- Tests must be deterministic: no sleeps, no network, no dependence on the current date.
- Before implementation the suite must fail because the stub panics; a test that passes
  against the stub is rejected.
- `go test -json` reports per-test actions; the adapter parses those, not the text.
