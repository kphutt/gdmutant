"""Tests for the shared test helpers in conftest.py."""

from pathlib import Path

import pytest
from conftest import copy_of_template


def test_a_template_is_built_inside_pytests_own_temporary_directory(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # pytest prunes its own directory on a later run, so a killed run cannot leave one behind.
    built: list[Path] = []
    copy_of_template(tmp_path / "copy", "test_conftest probe", built.append)
    assert built[0].is_relative_to(tmp_path_factory.getbasetemp())
