Node 20 LTS, TypeScript 5.6, vitest 2. The adapter generates `package.json` and
`tsconfig.json`; never edit them.

Layout

- `src/<component>/index.ts` — one module per component, one exported function per
  contract operation.
- `src/entities.ts` — shared contract entities as exported `interface`s.
- `tests/<component>.test.ts` — vitest specs.
- `node_modules/` — installed by `npm ci`; never committed.

Writing the spec

- State requirement ids (`R1`, `R2`, scenarios `S1`) and one exact example each.
- Name the exported function and its parameter/return types; entities keep their
  contract names.
- Keep functions pure; no I/O, network or clock unless the requirement names it.
- Portable components (`platform: "any"` or `linux`) import only Node built-ins and
  their own modules. Never `react`, `react-dom`, `react-native` or `expo`.
- Errors: throw `Error` subclasses with a stable message prefix; document which.
- Type mapping: `str` -> `string`, `int`/`float` -> `number`, `bool` -> `boolean`,
  `list[str]` -> `string[]`, `dict` -> `Record<string, string>`.

Out of scope

- No extra dependencies: the toolchain is pinned to TypeScript and vitest.
- No UI layer in portable components; keep views in a separate macOS-only layer.
