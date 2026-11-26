Failure signatures

- Compile error: gcc/clang print `src/x.c:12:5: error: ...` and CMake exits non-zero. The
  pipeline classifies these as BUILD_ERROR. `-Werror=implicit-function-declaration` and
  `-Werror=return-type` turn those two warnings into errors; other warnings are a lint
  gate, not a build failure.
- Link error: `undefined reference to 'foo'` or `ld: symbol(s) not found` -> BUILD_ERROR.
- Test failure: the build succeeds, the executable runs and exits non-zero without a
  sanitizer or stub marker -> TEST_FAIL.
- Stub failure: the run prints `not implemented: <name>` and aborts -> valid stub failure.
- Sanitizer report: `ERROR: AddressSanitizer` or `runtime error:` -> TEST_FAIL with the
  report attached.

Common messages and fixes

- `implicit declaration of function 'x'`: include the header that declares `x`.
- `undefined reference to 'x'`: the source is not in the target's source list, or the
  library is not linked; check the generated `CMakeLists.txt` (do not edit it).
- `control reaches end of non-void function`: every path must return a value.
- `expected ';' before ...`: a missing semicolon or brace.
- `conflicting types for 'x'`: the definition does not match the header.
- `multiple definition of 'x'`: the symbol is defined in more than one translation unit.
- `warning: unused parameter`: remove the parameter or cast it to `(void)`.
