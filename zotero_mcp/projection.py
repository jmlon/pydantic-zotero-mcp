"""Raw Zotero JSON -> compact models.

Pure functions, no I/O, so they are cheap to unit-test against recorded payloads.
Nothing here reaches back to the API: if a field is not in the payload it becomes
``None`` and disappears from the response.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .models import (
    ChildSummary,
    CollectionNode,
    Creator,
    ItemDetail,
    ItemSummary,
    Note,
)

#: Attachment content types Zotero can extract text from.
INDEXABLE_CONTENT_TYPES = frozenset(
    {"application/pdf", "application/epub+zip", "text/html", "text/plain"}
)

_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")

#: Where the "container" title lives, by item type. Zotero uses a different field
#: name per type and the model should not have to care which.
_PUBLICATION_FIELDS = (
    "publicationTitle",
    "bookTitle",
    "proceedingsTitle",
    "blogTitle",
    "dictionaryTitle",
    "encyclopediaTitle",
    "websiteTitle",
    "forumTitle",
    "repository",
    "repositoryLocation",
    "publisher",
)


def _first(data: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_year(
    data: dict[str, Any], meta: dict[str, Any] | None = None
) -> int | None:
    """Best-effort publication year.

    Zotero's ``date`` field is free text ("Spring 2019", "2019-04-12", "n.d."), so
    ``meta.parsedDate`` is preferred and a regex is the fallback.
    """
    parsed = (meta or {}).get("parsedDate")
    if isinstance(parsed, str):
        match = _YEAR_RE.search(parsed)
        if match:
            return int(match.group(1))
    raw = data.get("date")
    if isinstance(raw, str):
        match = _YEAR_RE.search(raw)
        if match:
            return int(match.group(1))
    return None


def collapse_creators(
    data: dict[str, Any], meta: dict[str, Any] | None = None
) -> str | None:
    """Collapse the creator array to a display string.

    Zotero's own ``meta.creatorSummary`` is used when present — it already applies
    the "et al." conventions.
    """
    summary = (meta or {}).get("creatorSummary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()

    creators = data.get("creators")
    if not isinstance(creators, list) or not creators:
        return None

    names = [_creator_name(c) for c in creators if isinstance(c, dict)]
    names = [n for n in names if n]
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def _creator_name(creator: dict[str, Any]) -> str | None:
    single = creator.get("name")
    if isinstance(single, str) and single.strip():
        return single.strip()
    last = (creator.get("lastName") or "").strip()
    first = (creator.get("firstName") or "").strip()
    if last and first:
        return f"{first} {last}"
    return last or first or None


def _full_creators(data: dict[str, Any]) -> list[Creator]:
    out: list[Creator] = []
    for entry in data.get("creators") or []:
        if not isinstance(entry, dict):
            continue
        name = _creator_name(entry)
        if name:
            out.append(
                Creator(
                    creator_type=entry.get("creatorType") or "author",
                    name=name,
                )
            )
    return out


def normalize_doi(value: str | None) -> str | None:
    """Reduce a DOI to a comparable form.

    Handles the shapes that show up in practice: bare DOIs, ``doi:`` prefixes, and
    full https://doi.org URLs.
    """
    if not value:
        return None
    text = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        text = text.removeprefix(prefix)
    text = text.strip()
    return text or None


def extract_doi(data: dict[str, Any]) -> str | None:
    """DOI from the DOI field, or from ``extra`` where Zotero often parks it."""
    direct = _first(data, "DOI", "doi")
    if direct:
        return normalize_doi(direct)

    extra = data.get("extra")
    if isinstance(extra, str):
        match = re.search(r"doi:\s*(\S+)", extra, flags=re.IGNORECASE)
        if match:
            return normalize_doi(match.group(1))
    return None


def strip_html(value: str | None) -> str:
    """Notes are HTML. Models read plain text more cheaply than markup."""
    if not value:
        return ""
    text = _TAG_RE.sub("\n", value)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = _WS_RE.sub("\n", text)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _parse_date_added(data: dict[str, Any]) -> date | None:
    raw = data.get("dateAdded")
    if isinstance(raw, str) and len(raw) >= 10:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def _tags(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for entry in data.get("tags") or []:
        if isinstance(entry, dict):
            tag = entry.get("tag")
            if isinstance(tag, str) and tag.strip():
                out.append(tag.strip())
        elif isinstance(entry, str) and entry.strip():
            out.append(entry.strip())
    return out


def is_indexable(content_type: str | None, link_mode: str | None) -> bool:
    """Whether Zotero could plausibly have indexed this attachment's text."""
    if link_mode == "linked_url":
        return False
    return (content_type or "") in INDEXABLE_CONTENT_TYPES


def item_summary(
    raw: dict[str, Any],
    *,
    collection_names: dict[str, str] | None = None,
) -> ItemSummary:
    """Project a raw Zotero item into an :class:`ItemSummary`."""
    data = raw.get("data") or {}
    meta = raw.get("meta") or {}
    item_type = data.get("itemType") or "unknown"

    names = collection_names or {}
    collections = [
        names.get(key, key) for key in (data.get("collections") or []) if key
    ]

    has_fulltext: bool | None = None
    if item_type == "attachment":
        has_fulltext = is_indexable(data.get("contentType"), data.get("linkMode"))
    elif not meta.get("numChildren"):
        # No children at all, so there is nothing that could carry indexed text.
        has_fulltext = False

    return ItemSummary(
        key=raw.get("key") or data.get("key") or "",
        item_type=item_type,
        title=_first(data, "title", "caseName", "subject") or "(untitled)",
        creators=collapse_creators(data, meta),
        year=extract_year(data, meta),
        publication=_first(data, *_PUBLICATION_FIELDS),
        doi=extract_doi(data),
        url=_first(data, "url"),
        tags=_tags(data),
        collections=collections,
        num_children=int(meta.get("numChildren") or 0),
        has_fulltext=has_fulltext,
        date_added=_parse_date_added(data),
    )


def item_detail(
    raw: dict[str, Any],
    *,
    collection_names: dict[str, str] | None = None,
    children: list[ChildSummary] | None = None,
) -> ItemDetail:
    """Project a raw Zotero item into an :class:`ItemDetail`."""
    data = raw.get("data") or {}
    summary = item_summary(raw, collection_names=collection_names)

    has_fulltext = summary.has_fulltext
    if children is not None:
        has_fulltext = any(child.may_have_fulltext for child in children)

    return ItemDetail(
        **summary.model_dump(exclude_none=False, exclude={"has_fulltext"}),
        has_fulltext=has_fulltext,
        abstract=_first(data, "abstractNote"),
        creator_list=_full_creators(data),
        publisher=_first(data, "publisher"),
        volume=_first(data, "volume"),
        issue=_first(data, "issue"),
        pages=_first(data, "pages", "numPages"),
        date_raw=_first(data, "date"),
        language=_first(data, "language"),
        extra=_first(data, "extra"),
        date_modified=_first(data, "dateModified"),
        version=raw.get("version") or data.get("version"),
        children=children,
    )


def child_summary(raw: dict[str, Any]) -> ChildSummary:
    """Project an attachment or child note."""
    data = raw.get("data") or {}
    item_type = data.get("itemType")
    is_note = item_type == "note"
    content_type = data.get("contentType")
    link_mode = data.get("linkMode")

    title = data.get("title")
    if is_note and not title:
        text = strip_html(data.get("note"))
        title = (text[:60] + "...") if len(text) > 60 else text or None

    return ChildSummary(
        key=raw.get("key") or "",
        child_type="note" if is_note else "attachment",
        title=title,
        content_type=content_type,
        link_mode=link_mode,
        filename=data.get("filename"),
        may_have_fulltext=(not is_note) and is_indexable(content_type, link_mode),
    )


def note(raw: dict[str, Any]) -> Note:
    """Project a child note."""
    data = raw.get("data") or {}
    return Note(
        key=raw.get("key") or "",
        text=strip_html(data.get("note")),
        tags=_tags(data),
        date_modified=data.get("dateModified"),
    )


def collection_tree(
    raw_collections: list[dict[str, Any]], *, include_counts: bool = True
) -> tuple[list[CollectionNode], int]:
    """Build a nested collection tree from Zotero's flat list.

    Orphans (children whose parent is not in the payload) are promoted to the top
    level rather than dropped — a partial page must not hide collections.
    """
    nodes: dict[str, CollectionNode] = {}
    parents: dict[str, str | None] = {}

    for raw in raw_collections:
        data = raw.get("data") or {}
        meta = raw.get("meta") or {}
        key = raw.get("key") or data.get("key")
        if not key:
            continue
        parent = data.get("parentCollection")
        parents[key] = parent if isinstance(parent, str) and parent else None
        nodes[key] = CollectionNode(
            key=key,
            name=data.get("name") or "(unnamed)",
            num_items=meta.get("numItems") if include_counts else None,
            num_subcollections=int(meta.get("numCollections") or 0),
        )

    roots: list[CollectionNode] = []
    for key, node in nodes.items():
        parent_key = parents.get(key)
        if parent_key and parent_key in nodes:
            nodes[parent_key].children.append(node)
        else:
            roots.append(node)

    def sort_recursive(items: list[CollectionNode]) -> None:
        items.sort(key=lambda n: n.name.lower())
        for item in items:
            sort_recursive(item.children)

    sort_recursive(roots)
    return roots, len(nodes)


def collection_name_map(raw_collections: list[dict[str, Any]]) -> dict[str, str]:
    """Key -> name, so item summaries can show names instead of opaque keys."""
    out: dict[str, str] = {}
    for raw in raw_collections:
        data = raw.get("data") or {}
        key = raw.get("key") or data.get("key")
        name = data.get("name")
        if key and name:
            out[key] = name
    return out
