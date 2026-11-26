# Captured Cargo/libtest output samples

These files were captured from a real toolchain (`cargo 1.90.0` / `rustc 1.90.0`
on Linux x86_64) against a throwaway workspace with one crate:

| File | Command | Exit | What it pins |
|------|---------|------|--------------|
| `build_error.txt` | `cargo build --workspace --message-format=short` | 101 | `path:line:col: error[E0308]: …` plus `error: could not compile …` |
| `test_pass.txt` | `cargo test -p demo --test demo_test -- --test-threads=1` | 0 | `test <name> ... ok` |
| `test_fail.txt` | the same on a failing assertion | 101 | `... FAILED`, the `---- <name> stdout ----` block and the trailing `error: test failed` |
| `stub_panic.txt` | the same against an `unimplemented!()` body | 101 | the panic message `not implemented` |
| `zero_tests.txt` | the same with every test filtered out | 0 | `running 0 tests` |

The pipeline itself builds with `--message-format=json`; these samples pin the
`short` format used by `validate_stub` and libtest's stable text output, which
does not change between Rust releases.
