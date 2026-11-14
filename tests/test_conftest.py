"""Tests for the shared test helpers in conftest.py."""

from pathlib import Path

import pytest
from conftest import copy_of_template, start_template_session


def test_a_template_is_built_inside_pytests_own_temporary_directory(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # pytest prunes its own directory on a later run, so a killed run cannot leave one behind.
    built: list[Path] = []
    copy_of_template(tmp_path / "copy", "test_conftest probe", built.append)
    assert built[0].is_relative_to(tmp_path_factory.getbasetemp())


def test_a_new_session_builds_its_templates_again(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    built: list[Path] = []
    copy_of_template(tmp_path / "first", "test_conftest session probe", built.append)
    try:
        start_template_session(tmp_path_factory.mktemp("next-session"))
        copy_of_template(tmp_path / "second", "test_conftest session probe", built.append)
    finally:
        start_template_session(tmp_path_factory.mktemp("templates"))
    assert len(built) == 2
    assert built[1].parent.name.startswith("next-session")
