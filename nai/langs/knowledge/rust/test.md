libtest via `cargo test --workspace`.

Shape

```rust
use word_counter::count_words;

#[test]
fn r1_count_words() {
    assert_eq!(count_words(" a  b "), 2);
}
```

Rules

- One integration test file per crate: `crates/<crate>/tests/<crate>_test.rs`.
- Name each test `r1_<behaviour>`; traceability matches the id after the first `_`.
- Use `assert_eq!`, `assert!`, `assert!(matches!(...))`.
- Return `Result<(), E>` from a test only when the error type implements `Debug`.
- Tests must be deterministic: no sleeps, no network, no dependence on the current date.
- Before implementation the suite must fail because the stub panics; a test that passes
  against the stub is rejected.
- libtest's text output is stable: `test r1_count_words ... ok` / `... FAILED`.
