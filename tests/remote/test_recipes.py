"""Recipe registry: parameter allowlists, argv rendering and template checks."""

from __future__ import annotations

import pytest

from nai.remote.errors import RecipeError
from nai.remote.recipes import RECIPES, check_template, render_argv, spec_for, validate_params


def test_known_recipes_are_registered() -> None:
    assert {"echo", "toolchain", "build", "lint", "verify", "integrate"} <= set(RECIPES)
    with pytest.raises(RecipeError, match="unknown recipe"):
        spec_for("rm -rf /")


def test_unknown_parameter_is_rejected() -> None:
    with pytest.raises(RecipeError, match="does not accept"):
        validate_params(spec_for("verify"), {"targets": ["tests/a.py"], "extra": "x"})


@pytest.mark.parametrize("value", ["../etc/passwd", "/etc/passwd", "a/../../b", "C:/x", "a\x00b"])
def test_unsafe_paths_are_rejected(value: str) -> None:
    with pytest.raises(RecipeError):
        validate_params(spec_for("verify"), {"targets": [value]})


@pytest.mark.parametrize("value", ["a; rm -rf /", "a && b", "$(id)", "`id`", "a|b", "a\nb", "'quoted'"])
def test_shell_metacharacters_are_rejected(value: str) -> None:
    with pytest.raises(RecipeError):
        validate_params(spec_for("echo"), {"message": value})


def test_paths_are_normalised() -> None:
    cleaned = validate_params(spec_for("verify"), {"targets": ["./tests//a.py", "pkg/./b.py"]})
    assert cleaned["targets"] == ["tests/a.py", "pkg/b.py"]


def test_required_parameter_must_be_present() -> None:
    with pytest.raises(RecipeError, match="requires parameter"):
        validate_params(spec_for("echo"), {})


def test_list_parameter_is_spliced_into_argv() -> None:
    argv = render_argv(
        spec_for("verify"),
        ["{python}", "-m", "pytest", "{targets}", "-q"],
        {"targets": ["tests/a.py", "tests/b.py"]},
        {"python": "python3"},
    )
    assert argv == ["python3", "-m", "pytest", "tests/a.py", "tests/b.py", "-q"]


def test_empty_list_placeholder_is_dropped() -> None:
    argv = render_argv(
        spec_for("verify"), ["{python}", "-m", "pytest", "{targets}", "-q"], {}, {"python": "python3"}
    )
    assert argv == ["python3", "-m", "pytest", "-q"]


def test_list_parameter_must_be_a_whole_argument() -> None:
    with pytest.raises(RecipeError, match="whole argument"):
        check_template(spec_for("verify"), ["pytest", "--targets={targets}"], ("python",))
    with pytest.raises(RecipeError, match="whole argument"):
        render_argv(spec_for("verify"), ["pytest", "--targets={targets}"], {"targets": ["a.py"]})


def test_template_rejects_unknown_placeholders() -> None:
    with pytest.raises(RecipeError, match="unknown placeholder"):
        check_template(spec_for("verify"), ["{python}", "{mystery}"], ("python",))


def test_template_rejects_shell_entries() -> None:
    with pytest.raises(RecipeError, match="may not invoke a shell"):
        check_template(spec_for("verify"), ["bash", "-c", "echo hi; rm -rf /"], ("python",))
    with pytest.raises(RecipeError, match="may not invoke a shell"):
        check_template(spec_for("verify"), ["/bin/sh", "-c", "true"], ("python",))
    with pytest.raises(RecipeError, match="empty"):
        check_template(spec_for("verify"), [], ("python",))


def test_recipe_capabilities_are_declared() -> None:
    assert spec_for("verify").capabilities == ("python",)
    assert spec_for("echo").capabilities == ()
