"""Tool behaviour over the in-memory transport."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.fake_zotero import FakeZotero, make_item
from zotero_mcp.gateway import ZoteroGateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------- schema surface


async def test_expected_read_tools_are_exposed(client):
    async with client:
        names = {tool.name for tool in await client.list_tools()}

    assert {
        "get_library_info",
        "search_items",
        "list_recent_items",
        "find_item_by_identifier",
        "get_item",
        "get_item_children",
        "get_item_notes",
        "get_item_fulltext",
        "list_collections",
        "list_collection_items",
        "list_tags",
    } <= names


async def test_write_tools_absent_when_writes_disabled(client):
    async with client:
        names = {tool.name for tool in await client.list_tools()}

    assert not {n for n in names if n.startswith(("create_", "update_", "add_"))}
    assert not {n for n in names if "delete" in n}


async def test_every_tool_has_a_description(client):
    async with client:
        tools = await client.list_tools()
    missing = [t.name for t in tools if not (t.description or "").strip()]
    assert not missing


async def test_every_tool_sets_all_four_hints(client):
    hints = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    async with client:
        tools = await client.list_tools()
    incomplete = {
        t.name: [h for h in hints if not isinstance(getattr(t.annotations, h, None), bool)]
        for t in tools
    }
    assert not {name: gaps for name, gaps in incomplete.items() if gaps}


# ------------------------------------------------------------------------ search


async def test_search_returns_projected_summaries(client, library):
    async with client:
        result = await client.call_tool("search_items", {"query": "attention"})

    data = result.structured_content
    assert data["returned"] == 2
    first = data["items"][0]
    assert first["key"] == "AAAA1111"
    assert first["creators"] == "Vaswani"
    assert "links" not in first

    params = library.calls_named("top")[0]
    assert params["q"] == "attention"
    assert params["qmode"] == "titleCreatorYear"


async def test_fulltext_mode_maps_to_qmode_everything(client, library):
    async with client:
        await client.call_tool("search_items", {"query": "x", "mode": "fulltext"})
    assert library.calls_named("top")[0]["qmode"] == "everything"


async def test_relevance_sort_omits_sort_parameter(client, library):
    async with client:
        await client.call_tool("search_items", {"query": "x"})
    assert "sort" not in library.calls_named("top")[0]

    async with client:
        await client.call_tool("search_items", {"query": "x", "sort": "title"})
    assert library.calls_named("top")[-1]["sort"] == "title"


async def test_empty_query_is_rejected_with_guidance(client):
    async with client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("search_items", {"query": "   "})
    assert "list_recent_items" in str(excinfo.value)


async def test_limit_above_hundred_is_rejected_by_schema(client):
    async with client:
        with pytest.raises(ToolError):
            await client.call_tool("search_items", {"query": "x", "limit": 500})


async def test_truncation_is_reported_not_hidden(settings):
    items = [make_item(f"KEY{i:05d}") for i in range(5)]
    library = FakeZotero(items=items, total_results=4210)
    gateway = ZoteroGateway(settings, client_factory=lambda: library)

    from fastmcp import Client

    from zotero_mcp.server import create_server

    async with Client(create_server(settings, gateway=gateway)) as client:
        result = await client.call_tool("search_items", {"query": "x", "limit": 5})

    data = result.structured_content
    assert data["returned"] == 5
    assert data["total_matched"] == 4210
    assert data["truncated"] is True
    assert data["next_start"] == 5
    assert "4210" in data["hint"]


async def test_collection_scoped_search_uses_collection_endpoint(client, library):
    async with client:
        await client.call_tool(
            "search_items", {"query": "x", "collection_key": "COLL0001"}
        )
    assert library.calls_named("collection_items_top")


async def test_bad_collection_key_is_rejected(client):
    async with client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "search_items", {"query": "x", "collection_key": "not-a-key"}
            )
    assert "8-character" in str(excinfo.value)


# ------------------------------------------------------------------------- items


async def test_get_item_returns_detail_with_abstract(client):
    async with client:
        result = await client.call_tool("get_item", {"item_key": "AAAA1111"})
    assert result.structured_content["abstract"] == "An abstract."
    assert result.structured_content["version"] == 100


async def test_get_item_with_children_resolves_fulltext_flag(client):
    async with client:
        result = await client.call_tool(
            "get_item", {"item_key": "AAAA1111", "include_children": True}
        )
    data = result.structured_content
    assert data["has_fulltext"] is True
    kinds = {c["child_type"] for c in data["children"]}
    assert kinds == {"attachment", "note"}


async def test_unknown_item_key_gives_actionable_error(client):
    async with client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("get_item", {"item_key": "ZZZZ9999"})
    message = str(excinfo.value)
    assert "not found" in message.lower()
    assert "search_items" in message


async def test_malformed_item_key_rejected_before_any_request(client, library):
    async with client:
        with pytest.raises(ToolError):
            await client.call_tool("get_item", {"item_key": "../../etc/passwd"})
    assert not library.calls_named("item")


async def test_notes_are_returned_as_plain_text(client):
    async with client:
        result = await client.call_tool("get_item_notes", {"item_key": "AAAA1111"})
    # A list-returning tool wraps its array as {"result": [...]}.
    notes = result.structured_content["result"]
    assert len(notes) == 1
    assert "<" not in notes[0]["text"]
    assert "self-attention" in notes[0]["text"]


# ---------------------------------------------------------------------- full text


async def test_fulltext_truncates_at_configured_ceiling(client):
    async with client:
        result = await client.call_tool("get_item_fulltext", {"item_key": "AAAA1111"})
    data = result.structured_content
    assert data["total_chars"] == 250_000
    assert data["returned_chars"] == 100_000  # the D2 default
    assert data["truncated"] is True
    assert data["attachment_key"] == "ATCH1111"


async def test_fulltext_per_call_max_chars_overrides_default(client):
    async with client:
        result = await client.call_tool(
            "get_item_fulltext", {"item_key": "AAAA1111", "max_chars": 5_000}
        )
    assert result.structured_content["returned_chars"] == 5_000
    assert result.structured_content["truncated"] is True


async def test_fulltext_resolves_parent_to_attachment(client, library):
    async with client:
        await client.call_tool("get_item_fulltext", {"item_key": "AAAA1111"})
    # Parent tried first, then children, then the attachment.
    tried = [args[0] for name, args, _ in library.calls if name == "fulltext_item"]
    assert tried == ["AAAA1111", "ATCH1111"]


async def test_fulltext_missing_explains_what_to_do_instead(client):
    async with client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("get_item_fulltext", {"item_key": "BBBB2222"})
    message = str(excinfo.value)
    assert "no indexed full text" in message.lower()
    assert "get_item" in message


# ---------------------------------------------------------------------- structure


async def test_collections_are_nested(client):
    async with client:
        result = await client.call_tool("list_collections", {})
    tree = result.structured_content
    assert tree["total"] == 3
    nlp = next(c for c in tree["collections"] if c["name"] == "NLP")
    assert [c["name"] for c in nlp["children"]] == ["Transformers"]


async def test_tags_are_sorted_and_filterable(client):
    async with client:
        result = await client.call_tool("list_tags", {})
        tags = result.structured_content["result"]
        assert [t["tag"] for t in tags] == ["nlp", "transformers", "vision"]

        filtered = await client.call_tool("list_tags", {"prefix": "tr"})
        assert [t["tag"] for t in filtered.structured_content["result"]] == [
            "transformers"
        ]


async def test_library_info_reports_mode_and_write_state(client):
    async with client:
        result = await client.call_tool("get_library_info", {})
    data = result.structured_content
    assert data["mode"] == "web"
    assert data["writes_enabled"] is False
    assert data["fulltext_max_chars"] == 100_000
    assert data["num_items"] == 2


# ------------------------------------------------------------------- match recall


async def test_identifier_lookup_by_key_is_exact(client):
    async with client:
        result = await client.call_tool(
            "find_item_by_identifier", {"identifier": "AAAA1111"}
        )
    assert result.structured_content["matched_on"] == "key"
    assert result.structured_content["confidence"] == 1.0


async def test_doi_match_is_reported_as_doi(client):
    async with client:
        result = await client.call_tool(
            "find_item_by_identifier",
            {"identifier": "https://doi.org/10.48550/arXiv.1706.03762"},
        )
    assert result.structured_content["matched_on"] == "doi"
    assert len(result.structured_content["candidates"]) == 1


async def test_duplicate_titles_return_both_candidates(settings):
    """A pre-print and its published version must both survive (PRD D3)."""
    preprint = make_item("PRE00001", title="Same Title", date="2020", doi=None)
    published = make_item("PUB00001", title="Same Title", date="2021", doi="10.1/x")
    library = FakeZotero(items=[preprint, published], total_results=2)
    gateway = ZoteroGateway(settings, client_factory=lambda: library)

    from fastmcp import Client

    from zotero_mcp.server import create_server

    async with Client(create_server(settings, gateway=gateway)) as client:
        result = await client.call_tool(
            "find_item_by_identifier", {"identifier": "Same Title"}
        )

    data = result.structured_content
    assert data["matched_on"] == "title"
    assert {c["key"] for c in data["candidates"]} == {"PRE00001", "PUB00001"}
    assert "caller should choose" in (data.get("note") or "")


async def test_absent_reference_reports_none_not_a_guess(settings):
    library = FakeZotero(items=[], total_results=0)
    gateway = ZoteroGateway(settings, client_factory=lambda: library)

    from fastmcp import Client

    from zotero_mcp.server import create_server

    async with Client(create_server(settings, gateway=gateway)) as client:
        result = await client.call_tool(
            "find_item_by_identifier", {"identifier": "A Fabricated Paper"}
        )

    data = result.structured_content
    assert data["matched_on"] == "none"
    assert data["confidence"] == 0.0
    assert data["candidates"] == []
