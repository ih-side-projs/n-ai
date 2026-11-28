Failure signatures

- Compile error: `cargo build --message-format=json` exits non-zero and emits a
  `compiler-message` with `level: "error"` and a `rendered` diagnostic. The pipeline
  classifies these as BUILD_ERROR. A plain `cargo build` prints `error[E0308]: ...`.
- Test failure: the build succeeds, then libtest prints `test r1_x ... FAILED` and exits
  non-zero. That is TEST_FAIL.
- Stub failure: the build succeeds and the test panics with `not implemented`; that is a
  valid pre-implementation failure.

Common messages and fixes

- `E0308: mismatched types`: fix the value or the annotation.
- `E0425: cannot find value/function`: missing import or a typo.
- `E0433: failed to resolve`: wrong crate name or missing dependency.
- `E0502: cannot borrow as mutable because it is also borrowed as immutable`: end the
  first borrow before mutating.
- `E0382: use of moved value`: clone or borrow instead of moving.
- `E0277: trait bound not satisfied`: the type does not implement the trait.
- `E0061: wrong number of arguments`: the call does not match the signature.
- `running 0 tests`: no tests were compiled (NO_TESTS).
