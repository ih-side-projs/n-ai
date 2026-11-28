- `NOT_IMPLEMENTED(name)` is deliberate in stubs; remove it when implementing.
- Do not edit `CMakeLists.txt`, `vendor/` or `include/entities/`; the adapter owns them.
- Tests that pass only because of undefined behaviour are a known risk: the sanitizer
  build (`-fsanitize=address,undefined`) catches many, but not all, of them.
- An out-of-bounds read/write is undefined behaviour; the sanitizer reports it as a
  TEST_FAIL with the report attached.
- Integer overflow on signed types is undefined; use `size_t` or check before adding.
- `char` signedness is platform-dependent; use `unsigned char` for byte values.
- `strcmp` returns 0 for equal strings, not true.
- `sizeof(pointer)` is the pointer size, not the array size; pass the length explicitly.
- `malloc` can return `NULL`; check it and free on every error path.
- A `char *` string literal is read-only; use `const char *`.
- Header guards prevent multiple inclusion; every public header needs one.
- Platform: `any`/`linux` components may not include Apple frameworks (verify fails).
