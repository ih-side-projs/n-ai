Read the first traceback frame that belongs to project code.

- `SyntaxError` / `IndentationError` -> the file does not parse; fix the reported line before anything else.
- `ModuleNotFoundError: No module named 'X'` -> wrong import path or the module is not written yet. Project modules live under `src/<package>/`; the package name comes from the contract.
- `ImportError: cannot import name 'Y'` -> the stub does not define `Y` or it was renamed; match the contract signature.
- `AttributeError: 'NoneType' object has no attribute ...` -> a function returned `None` on an error path that should raise.
- `TypeError: f() missing 1 required positional argument` -> the call site does not match the contract signature.
- `NameError` -> typo or use before assignment; check function and module scope.
- `RuntimeError: Event loop is closed` -> an async object reused after `asyncio.run` returned.
- pytest reports `no tests ran` -> test functions are not named `test_*` or the file sits outside `tests/`.
- `RuntimeError: Contract stub for X` -> the stub is still unimplemented; that is expected before implementation, a failure afterwards.
