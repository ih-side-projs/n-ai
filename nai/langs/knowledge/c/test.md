The vendored `nai_assert.h` harness, run through CTest.

Shape

```c
#include "nai_assert.h"
#include "word_counter/word_counter.h"

int main(void) {
    nai_begin("word_counter");
    nai_case("R1_counts_words");
    NAI_CHECK_EQ_INT(word_counter_count(" a  b "), 2);
    nai_case("R2_handles_empty");
    NAI_CHECK_EQ_INT(word_counter_count(""), 0);
    return nai_finish();
}
```

Rules

- One test executable per component: `tests/<component>/<component>_test.c` with `main`.
- Include only public headers (`include/<component>/...`) and `nai_assert.h`.
- `nai_begin(name)` starts the suite; `nai_case("R1_...")` starts a case;
  `NAI_CHECK(expr)`, `NAI_CHECK_EQ_INT`, `NAI_CHECK_STR_EQ` record failures;
  `return nai_finish();` prints JUnit XML to stdout and returns the failure count.
- Tests must be deterministic: no sleeps, no network, no dependence on the current date.
- Before implementation the suite must abort in the stub; a test that passes against the
  stub is rejected.
- Tests build with `-fsanitize=address,undefined` when the compiler supports it.
