C11, CMake 3.21 + CTest. The adapter generates `CMakeLists.txt`; never edit it.

Layout

- `include/<component>/<component>.h` — public header (the interface).
- `src/<component>/<component>.c` — implementation.
- `tests/<component>/<component>_test.c` — test executable with `main`.
- `include/entities/entities.h` — shared contract entities.
- `vendor/` — vendored single-header harness (`nai_assert.h`, `nai_runtime.h`).

Ownership and error handling (state these in the spec)

- Every contract that returns a pointer must say who owns it: "caller frees" or
  "borrowed, valid until X". Functions that allocate document the matching free function.
- No hidden globals: pass state through parameters or an explicit context struct.
- Error codes: return `int` (0 = success, non-zero = error) or an enum; document each.
- Every public header has an include guard and includes only what it needs.

Writing the spec

- State requirement ids and one exact example each.
- Name the function prefix (`<component>_<operation>`), parameters and return types.
- Portable components (`platform: "any"`) may not include Apple frameworks.

Out of scope

- Mixed C/C++ projects are rejected in v1. One language per project.
- No network at build or test time; no FetchContent.
