The vendored doctest framework, run through CTest.

Shape

```cpp
#include "doctest.h"
#include "word_counter/word_counter.h"

TEST_CASE("R1 counts words") {
    CHECK(word_counter::count(" a  b ") == 2);
}

TEST_CASE("R2 handles empty") {
    CHECK(word_counter::count("") == 0);
}
```

Rules

- One test executable per component: `tests/<component>/<component>_test.cpp`.
- Include only public headers (`include/<component>/...`) and `doctest.h`.
- `TEST_CASE("...")` starts a case; `CHECK(expr)` records a failure; `REQUIRE(expr)` stops
  the case on failure.
- Tests must be deterministic: no sleeps, no network, no dependence on the current date.
- Before implementation the suite must throw in the stub; a test that passes against the
  stub is rejected.
- Tests build with `-fsanitize=address,undefined` when the compiler supports it.
