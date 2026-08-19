"""Resource surface and template validation (PRD 9.6)."""

from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import McpError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_static_resources_are_registered(client):
    async with client:
        uris = {str(r.uri) for r in await client.list_resources()}

    assert "zotero://library/info" in uris
    assert "zotero://collections" in uris
    assert "zotero://schema/item-types" in uris


async def test_templates_are_registered(client):
    async with client:
        templates = {t.uriTemplate for t in await client.list_resource_templates()}

    assert "zotero://items/{item_key}" in templates
    assert "zotero://items/{item_key}/fulltext" in templates
    assert "zotero://collections/{collection_key}/items" in templates
    assert "zotero://schema/item-types/{item_type}/fields" in templates


async def test_item_resource_returns_projected_json(client):
    async with client:
        contents = await client.read_resource("zotero://items/AAAA1111")

    payload = json.loads(contents[0].text)
    assert payload["key"] == "AAAA1111"
    assert "links" not in payload


async def test_collections_resource_returns_tree(client):
    async with client:
        contents = await client.read_resource("zotero://collections")
    payload = json.loads(contents[0].text)
    assert payload["total"] == 3


async def test_fulltext_resource_is_plain_text_and_capped(client):
    async with client:
        contents = await client.read_resource("zotero://items/ATCH1111/fulltext")
    assert len(contents[0].text) == 100_000


async def test_malformed_key_is_rejected_before_upstream(client, library):
    async with client:
        with pytest.raises(Exception) as excinfo:
            await client.read_resource("zotero://items/nope/fulltext")
    assert "8-character" in str(excinfo.value)
    assert not library.calls_named("fulltext_item")


async def test_traversal_attempt_in_template_is_rejected(client, library):
    async with client:
        with pytest.raises(McpError):
            await client.read_resource("zotero://items/..%2F..%2Fetc%2Fpasswd")
    assert not library.calls_named("item")


async def test_unknown_item_key_raises_resource_error(client):
    async with client:
        with pytest.raises(Exception) as excinfo:
            await client.read_resource("zotero://items/ZZZZ9999")
    assert "ZZZZ9999" in str(excinfo.value)


async def test_unknown_item_type_is_rejected(client):
    async with client:
        with pytest.raises(Exception) as excinfo:
            await client.read_resource("zotero://schema/item-types/not-a-type/fields")
    assert "Unknown item type" in str(excinfo.value)
