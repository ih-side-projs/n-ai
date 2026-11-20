"""CORE-3: PathPolicy glob semantics."""
import pytest

from nai.policy import JobContext, PathPolicy, glob_match


@pytest.mark.parametrize("pat,rel,ok", [
    ("**", "a/b/c.py", True), ("*.py", "a.py", True), ("*.py", "pkg/a.py", False), ("**/*.py", "a.py", True),
    ("**/*.py", "pkg/sub/a.py", True), ("src/", "src/x/y.py", True), ("src/", "srcx/y.py", False),
    ("tests/test_?.py", "tests/test_1.py", True), ("tests/test_?.py", "tests/test_12.py", False),
    ("a/*/c", "a/b/c", True), ("a/*/c", "a/b/b/c", False), ("[ab].txt", "a.txt", True), ("[!ab].txt", "a.txt", False),
    ("./x/**", "x/y", True), ("a.b", "aXb", False),
])
def test_glob_match(pat, rel, ok):
    assert glob_match(pat, rel) is ok


def test_policy_writable_and_readonly():
    p = PathPolicy(writable=("src/**", "tests/**"), readonly=("tests/frozen/**", "**/conftest.py"))
    assert p.allows_write("src/a.py")
    assert not p.allows_write("docs/a.md")
    assert not p.allows_write("tests/frozen/test_R1.py")     # read-only beats writable
    assert not p.allows_write("tests/conftest.py")
    assert p.is_protected("tests/frozen/x") and not p.is_protected("src/a.py")


def test_policy_defaults_and_empty():
    assert PathPolicy.allow_all().allows_write("anything/at/all")
    assert not PathPolicy(writable=()).allows_write("a")


def test_job_context_test_commands():
    assert JobContext().test_commands() == []
    assert JobContext(test_command="pytest").test_commands() == ["pytest"]
    assert JobContext(test_command=["a", "b"]).test_commands() == ["a", "b"]
