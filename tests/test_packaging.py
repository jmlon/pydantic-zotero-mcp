"""This package's own distribution metadata (PRD §7.1, §10 M6).

Two things are being protected here, both of which are invisible at import time and would fail
only in someone else's environment:

1. **The declared dependency ranges.** This ships as an installable distribution, resolved from
   `pyproject.toml` and never from `uv.lock`, so the ranges are what a consumer actually gets. A
   floor-only pin silently hands them whatever released most recently — versions this code has
   never run against. The sibling `deep-research-harness` project learned this the hard way (two
   dependencies were resolving a major version past anything tested, with nothing noticing), so
   the same check lives here.

2. **The `deep_research.mcp_servers` entry point.** It is what makes this server usable as an
   in-memory tool source, and nothing in normal use imports it — a typo in `pyproject.toml`, or a
   rename of `build_server`, would surface as "bundled server 'zotero' is not installed" in a
   completely different project. Cheap to assert here; expensive to debug there.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
ENTRY_POINT_GROUP = "deep_research.mcp_servers"


def declared_requirements() -> list[Requirement]:
    raw = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    return [Requirement(r) for r in raw]


@pytest.mark.parametrize("requirement", declared_requirements(), ids=lambda r: r.name)
def test_installed_version_satisfies_the_declared_range(
    requirement: Requirement,
) -> None:
    installed = metadata.version(requirement.name)
    assert requirement.specifier.contains(installed, prereleases=True), (
        f"{requirement.name} {installed} is installed and the suite passes against it, but "
        f"pyproject declares {requirement.specifier} - so consumers would get an untested version"
    )


@pytest.mark.parametrize("requirement", declared_requirements(), ids=lambda r: r.name)
def test_every_dependency_has_an_upper_bound(requirement: Requirement) -> None:
    """`fastmcp` is the one that matters: FastMCP 4 changes task-augmented execution."""
    operators = {spec.operator for spec in requirement.specifier}
    assert operators & {"<", "<=", "==", "~="}, (
        f"{requirement.name} has no upper bound: an install will take any future release, "
        "including the one that changes an API this code depends on"
    )


def test_the_package_is_installed_rather_than_only_importable() -> None:
    """A standalone project, not a directory on someone else's `sys.path` (PRD §7.1).

    Before this became its own distribution it ran on the parent repository's environment via a
    `pythonpath` entry, which is exactly what made in-memory embedding in another tool impossible.
    """
    assert metadata.version("pydantic-zotero-mcp")


def test_the_deep_research_entry_point_resolves_to_a_zero_argument_factory() -> None:
    entries = {ep.name: ep for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    assert "zotero" in entries, f"no {ENTRY_POINT_GROUP!r} entry point named 'zotero'"

    factory = entries["zotero"].load()
    assert callable(factory)

    import inspect

    required = [
        p
        for p in inspect.signature(factory).parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert not required, (
        f"the host calls this as factory(); {required} would have to be supplied, and the host "
        "has no way to supply them"
    )


def factory():
    entries = {ep.name: ep for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    return entries["zotero"].load()


def test_the_factory_builds_a_server_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The host process's contract: credentials come from the environment it already loaded."""
    monkeypatch.chdir(tmp_path)  # see the .env note in the test below
    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123456")

    assert factory()().name == "Zotero"


def test_settings_come_from_a_dotenv_in_the_hosts_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ZoteroSettings` declares `env_file=".env"`, a path relative to the *process* CWD.

    That is load-bearing for in-memory embedding and easy to miss: an embedded server picks up the
    **host's** `.env`, not one next to this package. For `deep-research`, whose working directory is
    the researcher's project folder, that is exactly the documented behaviour — credentials live
    beside the model API keys — but it means two researchers sharing one install can point the same
    bundled server at two different libraries, and it means this factory's result depends on where
    the host was launched from.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    (tmp_path / ".env").write_text(
        "ZOTERO_API_KEY=from-dotenv\nZOTERO_LIBRARY_ID=999999\n"
    )

    from zotero_mcp import ZoteroSettings

    settings = ZoteroSettings()
    assert (settings.api_key, settings.library_id) == ("from-dotenv", "999999")


def test_the_factory_fails_loudly_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The host reports this as "failed to start" and aborts the run, which is the point.

    Run from an empty directory, so no `.env` supplies what the environment doesn't.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    monkeypatch.setenv("ZOTERO_LOCAL", "false")

    with pytest.raises(Exception):  # noqa: B017 - any startup refusal is acceptable; silence is not
        factory()()
