Tooling: pytest 8 or newer; the project pins `pytest==<version>` in `target_env.packages`.

Layout and names
- One file per module: `tests/test_<module>.py`, importing from the package (`from tiny_lib.text_normalizer import normalize_text`).
- Name each test after the requirement it proves: `def test_R1_normalize_text(): ...`; scenarios use `def test_S1_<slug>(): ...`.
- Plain `assert`; no custom test frameworks.

Patterns
- `with pytest.raises(ValueError): ...` for declared errors; assert on the message when the spec fixes it.
- `@pytest.mark.parametrize("raw,expected", [...])` for table-driven cases.
- `tmp_path` for filesystem tests; never write inside the repository.
- Fixtures for shared setup; keep them file-local unless reused.
- `pytest.approx` or `math.isclose` for floats.

Rules
- No network, no `sleep`, no randomness without a fixed seed.
- Tests must fail against the `NotImplementedError` stub; a passing test before implementation is rejected.
- One behaviour per test; the failure message must identify the requirement id.
- Import only the contract's public names.
