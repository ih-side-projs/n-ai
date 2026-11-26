Implement the bodies of the contract stubs; keep the declarations.

Shape

```go
func CountWords(text string) int {
	return len(strings.Fields(text))
}
```

Rules

- Keep the exported signatures; add unexported helpers freely.
- Return errors as the last value; wrap with `fmt.Errorf("...: %w", err)`.
- Prefer `strings.Fields`, `strings.TrimSpace`, `strconv` over manual loops.
- Use `errors.Is`/`errors.As` for error checks; never compare error strings.
- No `init()` side effects, no package-level mutable state.
- Document exported names with a comment starting with the name.
- Run `gofmt`; the lint gate checks formatting.
