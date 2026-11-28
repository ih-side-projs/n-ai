Implement the bodies of the contract stubs; keep the declarations.

Shape

```c
int word_counter_count(const char *text) {
    if (text == NULL) {
        return 0;
    }
    int count = 0;
    int in_word = 0;
    for (const char *p = text; *p != '\0'; ++p) {
        if (*p == ' ' || *p == '\t' || *p == '\n') {
            in_word = 0;
        } else if (!in_word) {
            in_word = 1;
            ++count;
        }
    }
    return count;
}
```

Rules

- Keep the exported signatures; make helpers `static`.
- Check every pointer for `NULL` before dereferencing; return an error code or a
  documented default.
- Use `size_t` for sizes and indices; never mix signed and unsigned.
- Bound every loop and buffer copy; prefer `snprintf` over `strcpy`.
- No hidden globals; pass state explicitly.
- Free everything you allocate on every path, including error paths.
- Build with `-Wall -Wextra`; warnings are a lint gate, not build failures.
