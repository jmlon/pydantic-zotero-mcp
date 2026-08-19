"""Startup, configuration, and gateway behaviour (PRD 9.4, 9.9)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.fake_zotero import FakeZotero, make_item
from zotero_mcp.__main__ import settings_from_args
from zotero_mcp.gateway import ZoteroGateway, ZoteroUnavailable
from zotero_mcp.settings import ConfigurationError, ZoteroSettings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ------------------------------------------------------------------ configuration


def test_web_mode_requires_key_and_library_id():
    with pytest.raises(ConfigurationError) as excinfo:
        ZoteroSettings(api_key=None, library_id=None).validate_for_startup()
    message = str(excinfo.value)
    assert "ZOTERO_API_KEY" in message
    assert "ZOTERO_LIBRARY_ID" in message


def test_non_numeric_library_id_is_rejected_with_explanation():
    with pytest.raises(ConfigurationError) as excinfo:
        ZoteroSettings(api_key="k", library_id="researcher").validate_for_startup()
    assert "numeric" in str(excinfo.value)


def test_local_mode_needs_no_credentials_and_forces_read_only():
    settings = ZoteroSettings(local=True, allow_writes=True)
    settings.validate_for_startup()
    assert settings.allow_writes is False


def test_http_without_token_refuses_to_boot():
    with pytest.raises(ConfigurationError) as excinfo:
        ZoteroSettings(
            api_key="k", library_id="1", transport="http", auth_token=None
        ).validate_for_startup()
    assert "ZOTERO_MCP_AUTH_TOKEN" in str(excinfo.value)


def test_http_with_token_is_accepted():
    ZoteroSettings(
        api_key="k", library_id="1", transport="http", auth_token="secret"
    ).validate_for_startup()


def test_default_fulltext_ceiling_is_one_hundred_thousand():
    assert ZoteroSettings(api_key="k", library_id="1").fulltext_max_chars == 100_000


# ------------------------------------------------------------------- CLI overrides


def test_cli_flags_override_environment(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "env-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "999")
    monkeypatch.setenv("ZOTERO_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("ZOTERO_MCP_PORT", "1234")

    settings = settings_from_args(
        ["--transport", "http", "--port", "9000", "--library-id", "123456"]
    )
    assert settings.transport == "http"
    assert settings.port == 9000
    assert settings.library_id == "123456"
    assert settings.api_key == "env-key"  # untouched by flags


def test_environment_used_when_no_flags(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "env-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "999")
    monkeypatch.setenv("ZOTERO_FULLTEXT_MAX_CHARS", "20000")

    settings = settings_from_args([])
    assert settings.library_id == "999"
    assert settings.fulltext_max_chars == 20_000


def test_http_transport_exits_nonzero_without_token(monkeypatch):
    """The refusal must be a non-zero exit, not a warning."""
    from zotero_mcp.__main__ import main

    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123456")
    monkeypatch.delenv("ZOTERO_MCP_AUTH_TOKEN", raising=False)

    assert main(["--transport", "http"]) == 2


# --------------------------------------------------------------- import purity


def test_importing_the_package_touches_no_network_and_needs_no_config():
    """A module that dials Zotero on import cannot be embedded in-memory (PRD 7.2)."""
    code = (
        "import socket\n"
        "def _fail(*a, **k):\n"
        "    raise AssertionError('network access during import')\n"
        "socket.socket.connect = _fail\n"
        "socket.create_connection = _fail\n"
        "import zotero_mcp\n"
        "import zotero_mcp.server, zotero_mcp.gateway, zotero_mcp.projection\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)},
        check=False,  # the returncode is the assertion below
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# ------------------------------------------------------------------- in-memory use


@pytest.mark.anyio
async def test_server_is_embeddable_with_injected_settings(library):
    """The documented in-memory embedding path (PRD 7.2)."""
    from fastmcp import Client

    from zotero_mcp.server import create_server

    settings = ZoteroSettings(api_key="k", library_id="123456")
    gateway = ZoteroGateway(settings, client_factory=lambda: library)

    async with Client(create_server(settings, gateway=gateway)) as client:
        result = await client.call_tool("search_items", {"query": "attention"})
    assert result.structured_content["returned"] == 2


# ----------------------------------------------------------------------- gateway


@pytest.mark.anyio
async def test_gateway_reads_total_results_header(settings):
    library = FakeZotero(items=[make_item("AAAA1111")], total_results=77)
    gateway = ZoteroGateway(settings, client_factory=lambda: library)

    fetched = await gateway.fetch(lambda z: z.top(limit=1))
    assert fetched.total_results == 77
    assert fetched.library_version == 500


@pytest.mark.anyio
async def test_gateway_retries_transient_failures_then_succeeds(settings, monkeypatch):
    import zotero_mcp.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "RETRY_DELAYS", (0.0, 0.0))

    class Flaky(FakeZotero):
        def __init__(self) -> None:
            super().__init__(items=[make_item("AAAA1111")], total_results=1)
            self.attempts = 0

        def top(self, **kwargs):
            self.attempts += 1
            if self.attempts < 2:
                raise ConnectError("connection reset")
            return super().top(**kwargs)

    class ConnectError(Exception):
        pass

    flaky = Flaky()
    gateway = ZoteroGateway(settings, client_factory=lambda: flaky)
    result = await gateway.call(lambda z: z.top(limit=1))
    assert flaky.attempts == 2
    assert len(result) == 1


@pytest.mark.anyio
async def test_gateway_gives_up_and_classifies_after_max_attempts(
    settings, monkeypatch
):
    import zotero_mcp.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "RETRY_DELAYS", (0.0, 0.0))

    library = FakeZotero(
        fail_with=RuntimeError("503 service unavailable"), fail_times=-1
    )
    gateway = ZoteroGateway(settings, client_factory=lambda: library)

    with pytest.raises(ZoteroUnavailable):
        await gateway.call(lambda z: z.top(limit=1))


@pytest.mark.anyio
async def test_cache_is_reused_while_library_version_is_unchanged(settings, library):
    gateway = ZoteroGateway(settings, client_factory=lambda: library)

    async def load():
        return await gateway.call(lambda z: z.collections(limit=100))

    first = await gateway.cached("collections_raw", load)
    second = await gateway.cached("collections_raw", load)

    assert first == second
    assert len(library.calls_named("collections")) == 1


@pytest.mark.anyio
async def test_cache_invalidates_when_library_version_moves(settings, library):
    gateway = ZoteroGateway(settings, client_factory=lambda: library)

    async def load():
        return await gateway.call(lambda z: z.collections(limit=100))

    await gateway.cached("collections_raw", load)
    library._library_version = 501
    gateway._version_checked_at = 0.0  # force a fresh probe

    await gateway.cached("collections_raw", load)
    assert len(library.calls_named("collections")) == 2
