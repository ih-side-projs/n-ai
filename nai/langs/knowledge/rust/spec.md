Rust 1.75 (edition 2021), Cargo workspace. The adapter generates every `Cargo.toml`;
never edit them.

Layout

- `crates/<crate>/src/lib.rs` — one library crate per component.
- `crates/entities/src/lib.rs` — shared contract entities as structs.
- `crates/<crate>/tests/<crate>_test.rs` — integration tests.
- `Cargo.toml` — workspace manifest; `crates/*/Cargo.toml` — crate manifests.

Writing the spec

- State requirement ids (`R1`, `R2`, scenarios `S1`) and one exact example each.
- Name the exported function and its parameter/return types; entities keep their
  contract names.
- Return `Result<T, E>` for fallible operations; never panic in library code.
- Portable components (`platform: "any"` or `linux`) depend only on the standard library.
  Never GUI crates (iced, slint, druid, gtk).
- Type mapping: `str` -> `String`, `int` -> `i64`, `float` -> `f64`, `bool` -> `bool`,
  `list[str]` -> `Vec<String>`, `dict` -> `HashMap<String, String>`.

Out of scope

- No third-party crates: the workspace is pinned to the standard library.
- No async runtimes or network at test time.
