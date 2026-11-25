Read the first `error:` line: Swift reports one diagnostic per problem.

Common messages and fixes

- `cannot find 'x' in scope`: missing declaration or missing `import <Core>`; check the
  name and the target's dependencies in `Package.swift` (do not edit it, fix the source).
- `cannot find type 'Input' in scope`: the entity lives in `<Package>Core`; add
  `import <Package>Core`.
- `no such module 'TextNormalizer'`: the integration target is missing a dependency on
  that component; the adapter derives dependencies from the component map, so check the
  node names rather than editing the manifest.
- `value of type 'X' has no member 'y'`: wrong type or a typo; check the declaration.
- `missing return in a function expected to return 'T'`: every path must return.
- `use of unresolved identifier 'try'` / `errors thrown from here are not handled`: add
  `try` or `try?`/`try!` (avoid `try!`).
- `expression is 'async' but is not marked with 'await'`: add `await`, or make the caller
  `async`.
- `capture of 'x' with non-Sendable type in a @Sendable closure`: copy to a `let` first or
  use an `actor`.
- `linker command failed` / `no such module 'SwiftUI'` on Linux: the component is portable;
  remove the UI import and move the view into the macOS-only layer.
- `target 'X' referenced in product ... has no sources`: the source file is missing; the
  stub step must create `Sources/<Target>/<Target>.swift` before the manifest is built.
