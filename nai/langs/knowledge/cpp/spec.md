C++17, CMake 3.21 + CTest. The adapter generates `CMakeLists.txt`; never edit it.

Layout

- `include/<component>/<component>.h` — public header (the interface).
- `src/<component>/<component>.cpp` — implementation.
- `tests/<component>/<component>_test.cpp` — doctest test file.
- `include/entities/entities.h` — shared contract entities.
- `vendor/` — vendored single-header doctest.

Design rules (state these in the spec)

- RAII: every resource is owned by an object whose destructor releases it.
- Smart pointers: `std::unique_ptr` for exclusive ownership, `std::shared_ptr` only
  when ownership is genuinely shared. No raw owning pointers.
- const correctness: mark methods and parameters `const` unless they mutate.
- Rule of zero: prefer classes that need no copy/move/destructor; if you must write one,
  follow the rule of five.
- Avoid unnecessary templates; prefer plain functions and `std::` containers.
- Header-only vs `.cpp` split: declarations in the header, definitions in the `.cpp`;
  keep headers include-safe.

Writing the spec

- State requirement ids and one exact example each.
- Name the namespace (`<component>`), functions, parameters and return types.
- Portable components (`platform: "any"`) may not include Apple frameworks.

Out of scope

- Mixed C/C++ projects are rejected in v1. One language per project.
- No network at build or test time; no FetchContent.
