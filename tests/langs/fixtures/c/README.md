# Captured C toolchain output samples

Captured from a real toolchain (CMake 4.2.3, GCC 15.2.0, GNU Make 4.4.1 on Linux
x86_64) against a throwaway C11 project built exactly like the adapter generates
it (`include/adder/adder.h`, `src/adder/adder.c`, `tests/adder/adder_test.c`,
vendored `nai_assert.h` / `nai_runtime.h`).

| File | Command | Exit | What it pins |
|------|---------|------|--------------|
| `compile_error.txt` | `cmake --build build` | 2 | `file:line:col: error: …` plus the make failure tail |
| `link_error.txt` | the same with an undefined symbol | 2 | `undefined reference to …` and `collect2: error: ld returned 1` |
| `junit_pass.xml` | `./build/adder_test` | 0 | the harness's `<testcase …/>` element |
| `junit_fail.xml` | the same on a failing check | 1 | `<failure message="…"/>` |
| `stub_abort.txt` | the same against `NOT_IMPLEMENTED(adder_add)` | 134 | the `not implemented: adder_add` marker on stderr |

The test binary prints JUnit XML on stdout, so the adapter runs it directly
instead of parsing CTest's prose. The stub aborts (SIGABRT), which is why the
expected exit code is 134 rather than 1.
