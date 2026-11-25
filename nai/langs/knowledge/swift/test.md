XCTest, run with `swift test --filter <Target>Tests`.

Shape

```swift
import XCTest
@testable import TextNormalizer

final class TextNormalizerTests: XCTestCase {
    func test_R1_normalizeText() throws {
        XCTAssertEqual(normalizeText(" a  b "), "a b")
    }
}
```

Rules

- One `final class <Target>Tests: XCTestCase` per test target, file
  `Tests/<Target>Tests/<Target>Tests.swift`.
- Name each method `func test_<REQ-ID>_<behaviour>()`: traceability matches the id between
  underscores (`test_R1_normalizeText`). Cover every requirement id and scenario id.
- `@testable import <Target>` to reach `internal`/`public` API; never import another
  component's test target.
- Use `XCTAssertEqual`, `XCTAssertTrue`, `XCTAssertNil`, `XCTAssertThrowsError`.
- Async code: `func test_R2_loads() async throws` and `await` the call.
- Tests must be deterministic: no sleeps, no network, no dependence on the current date.
- Before implementation the suite must fail because the stub traps; an assertion that
  fails for another reason is a broken test.
