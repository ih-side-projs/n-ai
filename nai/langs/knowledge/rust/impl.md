Implement the bodies of the contract stubs; keep the declarations.

Shape

```rust
pub fn count_words(text: &str) -> i64 {
    text.split_whitespace().count() as i64
}
```

Rules

- Keep the exported signatures; add private helpers freely.
- Return `Result<T, E>`; define a small error enum with `impl std::error::Error`.
- Prefer iterators (`split_whitespace`, `map`, `filter`, `collect`) over index loops.
- Borrow parameters (`&str`, `&[T]`) and return owned values (`String`, `Vec<T>`).
- No `unsafe`, no global mutable state, no `unwrap()` on fallible I/O in library code.
- Derive `Debug`, `Clone`, `PartialEq` where useful.
- Run `cargo fmt`; the lint gate checks formatting.
