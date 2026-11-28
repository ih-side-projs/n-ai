/* Minimal single-header assertion harness for the C adapter.
 *
 * Vendored in the nai repository and copied into the workspace by the adapter.
 * No network access, no dependencies, C11.
 *
 * Usage in a test file:
 *
 *     #include "nai_assert.h"
 *     #include "adder/adder.h"
 *
 *     int main(void) {
 *         nai_begin("adder");
 *         nai_case("R1_adds_two_numbers");
 *         NAI_CHECK_EQ_INT(adder_add(2, 3), 5);
 *         nai_case("R2_adds_negative");
 *         NAI_CHECK_EQ_INT(adder_add(-1, 1), 0);
 *         return nai_finish();
 *     }
 *
 * ``nai_finish`` prints a JUnit XML document to stdout (so ctest's
 * ``--output-on-failure`` shows it) and returns 0 when every case passed.
 */
#ifndef NAI_ASSERT_H
#define NAI_ASSERT_H

#include <stdio.h>
#include <string.h>

#define NAI_MAX_CASES 256

typedef struct {
    const char *name;
    const char *message;
    int failed;
} nai_case_result;

static nai_case_result nai_cases[NAI_MAX_CASES];
static int nai_case_count = 0;
static int nai_current = -1;
static const char *nai_suite_name = "tests";

static void nai_begin(const char *suite) {
    nai_suite_name = suite;
    nai_case_count = 0;
    nai_current = -1;
}

static void nai_case(const char *name) {
    if (nai_case_count >= NAI_MAX_CASES) {
        nai_current = -1;
        return;
    }
    nai_current = nai_case_count++;
    nai_cases[nai_current].name = name;
    nai_cases[nai_current].message = "";
    nai_cases[nai_current].failed = 0;
}

static void nai_fail(const char *expr, const char *file, int line) {
    if (nai_current < 0) {
        return;
    }
    nai_cases[nai_current].failed = 1;
    nai_cases[nai_current].message = expr;
    fprintf(stderr, "FAIL %s: %s (%s:%d)\n", nai_cases[nai_current].name, expr, file, line);
}

static int nai_finish(void) {
    int failures = 0;
    int index;
    for (index = 0; index < nai_case_count; index++) {
        if (nai_cases[index].failed) {
            failures++;
        }
    }
    printf("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    printf("<testsuites>\n");
    printf("  <testsuite name=\"%s\" tests=\"%d\" failures=\"%d\">\n",
           nai_suite_name, nai_case_count, failures);
    for (index = 0; index < nai_case_count; index++) {
        const nai_case_result *result = &nai_cases[index];
        if (result->failed) {
            printf("    <testcase name=\"%s\" classname=\"%s\">\n",
                   result->name, nai_suite_name);
            printf("      <failure message=\"%s\"/>\n", result->message);
            printf("    </testcase>\n");
        } else {
            printf("    <testcase name=\"%s\" classname=\"%s\"/>\n",
                   result->name, nai_suite_name);
        }
    }
    printf("  </testsuite>\n");
    printf("</testsuites>\n");
    return failures == 0 ? 0 : 1;
}

#define NAI_CHECK(cond) \
    do { if (!(cond)) { nai_fail(#cond, __FILE__, __LINE__); } } while (0)

#define NAI_REQUIRE(cond) \
    do { if (!(cond)) { nai_fail(#cond, __FILE__, __LINE__); return; } } while (0)

#define NAI_CHECK_EQ_INT(actual, expected) \
    do { \
        long nai_a_ = (long)(actual); \
        long nai_e_ = (long)(expected); \
        if (nai_a_ != nai_e_) { \
            char nai_buf_[128]; \
            snprintf(nai_buf_, sizeof nai_buf_, "%s == %s (got %ld, want %ld)", \
                     #actual, #expected, nai_a_, nai_e_); \
            nai_fail(nai_buf_, __FILE__, __LINE__); \
        } \
    } while (0)

#define NAI_CHECK_STR_EQ(actual, expected) \
    do { \
        const char *nai_a_ = (actual); \
        const char *nai_e_ = (expected); \
        if (nai_a_ == NULL || nai_e_ == NULL || strcmp(nai_a_, nai_e_) != 0) { \
            char nai_buf_[256]; \
            snprintf(nai_buf_, sizeof nai_buf_, "%s == %s (got %s, want %s)", \
                     #actual, #expected, nai_a_ ? nai_a_ : "(null)", \
                     nai_e_ ? nai_e_ : "(null)"); \
            nai_fail(nai_buf_, __FILE__, __LINE__); \
        } \
    } while (0)

#endif /* NAI_ASSERT_H */
