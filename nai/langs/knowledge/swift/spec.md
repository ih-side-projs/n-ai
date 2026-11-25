Swift 6, SwiftPM 5.9 manifest, XCTest.

Package shape (generated; do not edit `Package.swift`)

- One library target per component: `Sources/<Target>/<Target>.swift`.
- Shared contract entities: `Sources/<Package>Core/Entities.swift`.
- Tests: `Tests/<Target>Tests/<Target>Tests.swift`.
- Integration nodes wire their siblings in `<Parent>Integration` and expose `integrate()`.

Writing the spec

- State the requirement ids (`R1`, `R2`, scenarios `S1`) the tests must cover.
- Name the target and the public API: function names, parameter types, return types.
- Prefer value types (`struct`, `enum`) and pure functions; no singletons or global state.
- Keep logic portable: a component whose platform is `any` or `linux` may import only
  Foundation and the standard library. Never SwiftUI, UIKit or AppKit.
- Put SwiftUI views in a separate, thin layer that only binds to view models; view models
  must not import UI frameworks.
- Name types and members in camelCase, types in PascalCase, one type per file where practical.

Contract mapping

- `str` -> `String`, `int` -> `Int`, `float` -> `Double`, `bool` -> `Bool`, `list[str]` -> `[String]`.
- A contract entity becomes a `public struct` with `public let` fields and an `init`.
- A component's `provides` entries become `public func` declarations with `_` argument labels.

Out of scope

- No third-party packages: do not add dependencies to the manifest.
- No Xcode project generation: the owner may supply one under `external/`.
