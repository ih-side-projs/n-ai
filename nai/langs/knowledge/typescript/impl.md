Implement the bodies of the contract stubs; keep the declarations.

Shape

```ts
export function countWords(text: string): number {
  return text.trim().split(/\s+/).filter(Boolean).length;
}
```

Rules

- Keep the exported signatures; add non-exported helpers freely.
- Prefer `const`, explicit return types on exported functions and no `any`.
- Use `unknown` for untrusted input and narrow with type guards.
- Strings: `trim`, `split`, `replace`, `normalize("NFC")`; avoid index arithmetic.
- Numbers: `Number.isInteger`, `Number.isFinite`; never compare floats with `===`.
- Errors: `throw new RangeError("...")`; catch only the narrow type.
- No I/O, network or global mutable state in library code.
- Keep `src/entities.ts` imports type-only (`import type`).
