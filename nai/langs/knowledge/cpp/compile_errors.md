Failure signatures

- Compile error: g++/clang++ print `src/x.cpp:12:5: error: ...` and CMake exits non-zero. The
  pipeline classifies these as BUILD_ERROR. `-Werror=return-type` turns a missing return
  into an error; other warnings are a lint gate, not a build failure.
- Link error: `undefined reference to 'foo'` or `ld: symbol(s) not found` -> BUILD_ERROR.
- Test failure: the build succeeds, the executable runs and exits non-zero without a
  sanitizer or stub marker -> TEST_FAIL.
- Stub failure: the run throws `std::logic_error("not implemented")` -> valid stub failure.
- Sanitizer report: `ERROR: AddressSanitizer` or `runtime error:` -> TEST_FAIL with the
  report attached.

Common messages and fixes

- `undefined reference to 'x'`: the source is not in the target's source list, or the
  library is not linked; check the generated `CMakeLists.txt` (do not edit it).
- `no matching function for call to 'x'`: the argument types do not match the declaration.
- `'x' was not declared in this scope`: missing `#include` or a typo.
- `control reaches end of non-void function`: every path must return a value.
- `multiple definition of 'x'`: a non-inline function is defined in a header.
- `cannot convert 'x' to 'y'`: the types are incompatible; add an explicit conversion.
- `use of undeclared identifier`: missing `std::` qualifier or include.
