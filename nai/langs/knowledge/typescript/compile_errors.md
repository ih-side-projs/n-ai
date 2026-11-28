Failure signatures

- Compile error: `tsc --noEmit` exits non-zero and prints `src/x.ts(3,7): error TS2322: ...`.
  The pipeline classifies these as BUILD_ERROR. The first `error TS####:` line names the
  file, line and column.
- Test failure: the build succeeds, then vitest exits non-zero with a JSON report whose
  `assertionResults` contain `status: "failed"` and a `failureMessages` entry. That is
  TEST_FAIL.
- Stub failure: the build succeeds and the test throws `Error: not implemented`; that is a
  valid pre-implementation failure.

Common messages and fixes

- `TS2307: Cannot find module '...'`: wrong relative path or missing `.js` extension.
- `TS2322: Type 'X' is not assignable to type 'Y'`: fix the value or the declared type.
- `TS2345: Argument of type ... is not assignable`: the call does not match the signature.
- `TS7006: Parameter 'x' implicitly has an 'any' type`: annotate the parameter.
- `TS18048: 'x' is possibly 'undefined'`: guard with `if (x === undefined)` or use `?.`.
- `TS2339: Property 'y' does not exist on type 'X'`: wrong type or a typo.
- `No test files found`: the spec path is wrong; vitest ran zero tests (NO_TESTS).
