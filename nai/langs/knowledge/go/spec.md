Go 1.22, standard library only. The adapter generates `go.mod`; never edit it.

Layout

- `internal/<component>/<component>.go` — one package per component, one exported
  function per contract operation.
- `internal/entities/entities.go` — shared contract entities as structs.
- `internal/<component>/<component>_test.go` — tests in the same package.
- `go.mod` — module path from `settings.go.module` or the project name.

Writing the spec

- State requirement ids (`R1`, `R2`, scenarios `S1`) and one exact example each.
- Name the exported function and its parameter/return types; entities keep their
  contract names.
- Return errors as the last value (`(T, error)`); never panic in library code.
- Portable components (`platform: "any"` or `linux`) import only the standard library.
  Never GUI toolkits (fyne, andlabs/ui, lxn/walk, therecipe/qt).
- Type mapping: `str` -> `string`, `int` -> `int`, `float` -> `float64`, `bool` -> `bool`,
  `list[str]` -> `[]string`, `dict` -> `map[string]string`.

Out of scope

- No third-party modules: the toolchain is pinned to the Go standard library.
- No cgo and no network at test time.
