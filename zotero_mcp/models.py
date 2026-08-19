"""Response models.

These exist to keep responses small. Raw Zotero item JSON is 2-4 KB per item, most of
it ``links``/``library``/empty type fields that cost the model tokens and tell it
nothing (PRD 6). Every model here is a deliberate projection, and ``CompactModel``
drops null fields on serialization so absent metadata costs nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CompactModel(BaseModel):
    """Base model that omits null fields when serialized.

    Pydantic has no ``exclude_none`` model setting, so the dump methods are
    overridden. Return-type annotations still yield a full output schema, so the
    model knows which fields *may* appear.
    """

    model_config = ConfigDict(populate_by_name=True)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("exclude_none", True)
        return super().model_dump_json(**kwargs)


# --------------------------------------------------------------------------- items


class ItemSummary(CompactModel):
    """Compact item projection used in every list/search response."""

    key: str = Field(description="8-character Zotero key; use it for follow-up calls")
    item_type: str = Field(description="Zotero item type, e.g. 'journalArticle'")
    title: str
    creators: str | None = Field(
        default=None, description="Collapsed creator summary, e.g. 'Vaswani et al.'"
    )
    year: int | None = None
    publication: str | None = Field(
        default=None, description="Journal, book, or repository name"
    )
    doi: str | None = None
    url: str | None = None
    tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(
        default_factory=list, description="Collection names (not keys)"
    )
    num_children: int = Field(
        default=0, description="Attachments plus notes, per Zotero's numChildren"
    )
    has_fulltext: bool | None = Field(
        default=None,
        description=(
            "True/False when known — for attachment items, or after "
            "get_item(include_children=True). Null means undetermined: call "
            "get_item_fulltext to find out."
        ),
    )
    date_added: date | None = None


class Creator(CompactModel):
    creator_type: str
    name: str


class ItemDetail(ItemSummary):
    """Full metadata for a single item."""

    abstract: str | None = None
    creator_list: list[Creator] = Field(default_factory=list)
    publisher: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    date_raw: str | None = Field(
        default=None, description="Zotero's unparsed date string"
    )
    language: str | None = None
    extra: str | None = None
    date_modified: str | None = None
    version: int | None = Field(
        default=None, description="Library version; required for any future write"
    )
    children: list[ChildSummary] | None = None


class ChildSummary(CompactModel):
    """An attachment or child note."""

    key: str
    child_type: Literal["attachment", "note"]
    title: str | None = None
    content_type: str | None = Field(
        default=None, description="MIME type for attachments, e.g. 'application/pdf'"
    )
    link_mode: str | None = Field(
        default=None,
        description="imported_file, imported_url, linked_file, or linked_url",
    )
    filename: str | None = None
    may_have_fulltext: bool = Field(
        default=False,
        description="True when this attachment is a type Zotero can index",
    )


class Note(CompactModel):
    """A note authored by the library owner."""

    key: str
    text: str = Field(description="Note content, HTML stripped to plain text")
    tags: list[str] = Field(default_factory=list)
    date_modified: str | None = None


class FullTextResult(CompactModel):
    """Indexed attachment text for one item."""

    item_key: str = Field(description="The key that was requested")
    attachment_key: str | None = Field(
        default=None, description="Attachment the text actually came from"
    )
    text: str
    total_chars: int = Field(description="Length of the full indexed text")
    returned_chars: int
    truncated: bool
    indexed_pages: int | None = None
    total_pages: int | None = None


# ------------------------------------------------------------------------- results


class SearchResults(CompactModel):
    """A bounded page of items, honest about what it left out."""

    items: list[ItemSummary]
    returned: int
    total_matched: int | None = Field(
        default=None, description="Total matches server-side, from Total-Results"
    )
    truncated: bool = Field(
        default=False, description="True when more matches exist than were returned"
    )
    next_start: int | None = Field(
        default=None, description="Pass as `start` to fetch the next page"
    )
    hint: str | None = Field(
        default=None, description="How to narrow or continue, when truncated"
    )


MatchBasis = Literal["key", "doi", "identifier", "title+year", "title", "none"]


class CitationMatch(CompactModel):
    """Candidate matches for a reference. The caller decides identity (PRD D3).

    Recall-oriented on purpose: a pre-print and its published version are both
    returned when both are present. The server reports the evidence and does not
    adjudicate.
    """

    query: str
    matched_on: MatchBasis
    confidence: float = Field(ge=0.0, le=1.0)
    candidates: list[ItemSummary] = Field(default_factory=list)
    note: str | None = None


# ------------------------------------------------------------------------ library


class CollectionNode(CompactModel):
    key: str
    name: str
    num_items: int | None = None
    num_subcollections: int = 0
    children: list[CollectionNode] = Field(default_factory=list)


class CollectionTree(CompactModel):
    collections: list[CollectionNode]
    total: int


class TagInfo(CompactModel):
    tag: str
    num_items: int | None = None


class LibraryInfo(CompactModel):
    """Cheap orientation call — what this library is and what may be done to it."""

    library_type: Literal["user", "group"]
    library_id: str
    mode: Literal["web", "local"] = Field(
        description="'local' reads the Zotero desktop API; it is always read-only"
    )
    num_items: int | None = Field(default=None, description="Top-level item count")
    num_collections: int | None = None
    last_modified_version: int | None = None
    writes_enabled: bool
    fulltext_max_chars: int
    default_style: str
    username: str | None = None


class BatchResult(CompactModel):
    """Partial-success shape mirroring Zotero's successful/unchanged/failed."""

    succeeded: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)


ItemDetail.model_rebuild()
CollectionNode.model_rebuild()
