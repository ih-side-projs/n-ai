# Captured C++ toolchain output samples

Captured from a real toolchain (CMake 4.2.3, GCC 15.2.0, doctest 2.4.11 on Linux
x86_64) against a throwaway C++17 project built exactly like the adapter
generates it (`include/adder/adder.h`, `src/adder/adder.cpp`,
`tests/adder/adder_test.cpp`, `tests/doctest_main.cpp`, vendored `doctest.h`).

| File | Command | Exit | What it pins |
|------|---------|------|--------------|
| `compile_error.txt` | `cmake --build build` | 2 | `file:line:col: error: …` plus the make failure tail |
| `link_error.txt` | the same with an undefined symbol | 2 | `undefined reference to …` and `collect2: error: ld returned 1` |
| `junit_pass.xml` | `./build/adder_test --reporters=junit` | 0 | doctest's JUnit `<testcase …/>` with `name`/`classname` |
| `junit_fail.xml` | the same on a failing `CHECK` | 1 | `<failure message="…" type="CHECK">` |
| `stub_throw.xml` | the same against a `std::logic_error("not implemented")` stub | 1 | `<failure type="CHECK">` whose body carries the marker |

doctest's JUnit reporter writes to stdout, so the adapter runs the test binary
directly with `--reporters=junit` rather than parsing CTest's prose.
