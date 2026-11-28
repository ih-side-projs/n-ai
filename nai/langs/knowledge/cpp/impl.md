Implement the bodies of the contract stubs; keep the declarations.

Shape

```cpp
namespace word_counter {
int count(const std::string &text) {
    int count = 0;
    bool in_word = false;
    for (char c : text) {
        if (c == ' ' || c == '\t' || c == '\n') {
            in_word = false;
        } else if (!in_word) {
            in_word = true;
            ++count;
        }
    }
    return count;
}
}
```

Rules

- Keep the exported signatures; make helpers `static` or in an anonymous namespace.
- Use RAII and smart pointers; never own a resource through a raw pointer.
- Be const-correct; mark methods and parameters `const` unless they mutate.
- Prefer the rule of zero; if you need a destructor, write the rule of five.
- Avoid unnecessary templates; prefer plain functions and `std::` containers.
- No hidden globals; pass state explicitly.
- Build with `-Wall -Wextra`; warnings are a lint gate, not build failures.
