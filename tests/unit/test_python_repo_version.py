"""Repo-declared Python version discovery (pinned-revision metadata only)."""

from __future__ import annotations

import pytest

from investigation_agent_platform.application.analysis.python.repo_version import (
    parse_version_file,
    resolve_repo_python_version,
    specifier_lower_bound,
)


def test_specifier_lower_bound():
    assert specifier_lower_bound(">=3.10") == (3, 10)
    assert specifier_lower_bound(">=3.9,<3.14") == (3, 9)
    assert specifier_lower_bound("~=3.11") == (3, 11)
    assert specifier_lower_bound("==3.12.*") == (3, 12)
    assert specifier_lower_bound("!=3.9") is None
    assert specifier_lower_bound("") is None


def test_python_version_file():
    assert parse_version_file(".python-version", "3.11.2\n") == (3, 11)
    assert parse_version_file(".python-version", "# comment\n\n3.10\n") == (3, 10)
    assert parse_version_file(".python-version", "") is None
    assert parse_version_file(".python-version", "not-a-version") is None


def test_pyproject_requires_python():
    content = '[project]\nname = "x"\nrequires-python = ">=3.10"\n'
    assert parse_version_file("pyproject.toml", content) == (3, 10)
    assert parse_version_file("pyproject.toml", '[project]\nname = "x"\n') is None
    assert parse_version_file("pyproject.toml", "not toml [[[\n") is None
    # Numbered evidence lines are tolerated.
    numbered = '1| [project]\n2| requires-python = ">=3.9"\n'
    assert parse_version_file("pyproject.toml", numbered) == (3, 9)


def test_setup_cfg_python_requires():
    content = "[options]\npython_requires = >=3.8\n"
    assert parse_version_file("setup.cfg", content) == (3, 8)
    assert parse_version_file("setup.cfg", "[options]\n") is None
    assert parse_version_file("setup.cfg", "garbage [[[\n:") is None


def test_unknown_filename_rejected():
    assert parse_version_file("setup.py", "x = 1") is None


@pytest.mark.asyncio
async def test_first_parseable_file_wins():
    async def reader(name):
        return {
            ".python-version": None,
            "pyproject.toml": '[project]\nrequires-python = ">=3.10"\n',
            "setup.cfg": "[options]\npython_requires = >=3.8\n",
        }[name]

    assert await resolve_repo_python_version(reader) == (3, 10)


@pytest.mark.asyncio
async def test_exact_pin_preferred_over_build_metadata():
    async def reader(name):
        return "3.12" if name == ".python-version" else '[project]\nrequires-python = ">=3.10"\n'

    assert await resolve_repo_python_version(reader) == (3, 12)


@pytest.mark.asyncio
async def test_nothing_usable_yields_none():
    async def reader(name):
        if name == ".python-version":
            raise FileNotFoundError(name)
        return ""

    assert await resolve_repo_python_version(reader) is None
