"""Shared fixtures. In-memory transport, no subprocess, no network (PRD 9)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fake_zotero import (
    FakeZotero,
    make_attachment,
    make_collection,
    make_item,
    make_note,
)
from zotero_mcp.gateway import ZoteroGateway
from zotero_mcp.settings import ZoteroSettings


@pytest.fixture
def settings() -> ZoteroSettings:
    """Valid settings that never touch the environment."""
    return ZoteroSettings(
        api_key="test-key",
        library_id="123456",
        library_type="user",
        transport="stdio",
        allow_writes=False,
    )


@pytest.fixture
def library() -> FakeZotero:
    """A small library: two papers, one with a PDF and a note."""
    items = [
        make_item(
            "AAAA1111",
            title="Attention Is All You Need",
            creators=[
                {"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"}
            ],
            date="2017-06-12",
            doi="10.48550/arXiv.1706.03762",
            tags=["transformers", "nlp"],
            collections=["COLL0001"],
            num_children=2,
        ),
        make_item(
            "BBBB2222",
            title="Deep Residual Learning",
            date="2015",
            tags=["vision"],
            collections=["COLL0002"],
            num_children=0,
        ),
    ]
    return FakeZotero(
        items=items,
        collections=[
            make_collection("COLL0001", "NLP", num_items=1),
            make_collection("COLL0002", "Vision", num_items=1),
            make_collection("COLL0003", "Transformers", parent="COLL0001", num_items=0),
        ],
        children={
            "AAAA1111": [
                make_attachment("ATCH1111", parent="AAAA1111"),
                make_note("NOTE1111", text="<p>Key idea: <b>self-attention</b>.</p>"),
            ],
            "BBBB2222": [],
        },
        fulltext={
            "ATCH1111": {
                "content": "x" * 250_000,
                "indexedPages": 9,
                "totalPages": 9,
            }
        },
        tags=["transformers", "nlp", "vision"],
        total_results=2,
    )


@pytest.fixture
def gateway(settings: ZoteroSettings, library: FakeZotero) -> ZoteroGateway:
    return ZoteroGateway(settings, client_factory=lambda: library)


@pytest.fixture
def server(settings: ZoteroSettings, gateway: ZoteroGateway):
    from zotero_mcp.server import create_server

    return create_server(settings, gateway=gateway)


@pytest.fixture
def client(server):
    """A FastMCP in-memory client bound to the test server."""
    from fastmcp import Client

    return Client(server)


__all__ = [
    "FakeZotero",
    "make_attachment",
    "make_collection",
    "make_item",
    "make_note",
]
