vitest 2, run with `npx vitest run --reporter=json --no-color <file>`.

Shape

```ts
import { describe, expect, it } from "vitest";
import { normalizeText } from "../src/text-normalizer/index.js";

describe("text-normalizer", () => {
  it("R1_normalizeText", () => {
    expect(normalizeText(" a  b ")).toBe("a b");
  });
});
```

Rules

- One file per component: `tests/<component>.test.ts`.
- Name each case `it("R1_<behaviour>", ...)`; traceability matches the id before the
  first underscore. Cover every requirement id and scenario id.
- Import the component through its public module; never reach into another
  component's internals.
- Use `expect(...).toBe`, `toEqual`, `toThrow`, `toHaveLength`.
- Async: `it("R2_loads", async () => { await load(); })`.
- Tests must be deterministic: no sleeps, no network, no dependence on the current date.
- Before implementation the suite must fail because the stub throws; a test that
  passes against the stub is rejected.
