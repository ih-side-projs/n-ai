Implement the bodies of the contract stubs; keep the declarations.

Shape

```swift
public func countWords(_ text: String) -> Int {
    text.split(whereSeparator: \.isWhitespace).count
}
```

Rules

- Keep the target's public signatures; add `private` helpers freely.
- Prefer `let` over `var`, `struct` over `class`, and early `guard` returns.
- Optionals: `guard let value = optional else { return fallback }`; never force-unwrap.
- Errors: declare `throws` only if the contract says so; use `enum XError: Error`.
- Strings: iterate `text.split(separator:)` or `text.unicodeScalars`, not integer indexes.
- Collections: `map`, `filter`, `reduce`, `first(where:)`; avoid index arithmetic.
- Concurrency: mark pure helpers `nonisolated`; use `async`/`await` for I/O; make value
  types `Sendable` implicitly by keeping them immutable.
- Do not import SwiftUI, UIKit or AppKit in portable code; keep views in a thin layer.
- No I/O, network or global mutable state in library code.
