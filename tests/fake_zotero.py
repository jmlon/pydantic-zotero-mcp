"""A fake pyzotero client for tests.

Mirrors the parts of pyzotero's contract that the gateway depends on, including the
awkward one: response metadata such as ``Total-Results`` is read back off
``client.request.headers`` after the call, not returned from it.
"""

from __future__ import annotations

from typing import Any


class FakeResponse:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


def make_item(
    key: str,
    *,
    title: str = "A Paper",
    item_type: str = "journalArticle",
    creators: list[dict[str, str]] | None = None,
    date: str | None = "2019-04-12",
    doi: str | None = None,
    tags: list[str] | None = None,
    collections: list[str] | None = None,
    num_children: int = 0,
    publication: str | None = "Journal of Things",
    abstract: str | None = "An abstract.",
    extra: str | None = None,
    version: int = 100,
) -> dict[str, Any]:
    """Build a raw item payload shaped like the real API's."""
    data: dict[str, Any] = {
        "key": key,
        "version": version,
        "itemType": item_type,
        "title": title,
        "creators": creators
        if creators is not None
        else [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
        "abstractNote": abstract,
        "publicationTitle": publication,
        "volume": "12",
        "issue": "3",
        "pages": "1-20",
        "date": date,
        "language": "en",
        "DOI": doi,
        "url": "https://example.org/paper",
        "extra": extra,
        "tags": [{"tag": t} for t in (tags or [])],
        "collections": collections or [],
        "dateAdded": "2024-03-01T10:00:00Z",
        "dateModified": "2024-03-02T10:00:00Z",
        # Fields that are pure noise for a model, and must not survive projection.
        "accessDate": "",
        "seriesTitle": "",
        "archiveLocation": "",
        "callNumber": "",
        "rights": "",
    }
    return {
        "key": key,
        "version": version,
        "library": {"type": "user", "id": 123456, "name": "noise"},
        "links": {"self": {"href": "https://api.zotero.org/x", "type": "app/json"}},
        "meta": {
            # Derived from the creators actually passed in, the way the real API does
            # it — a hardcoded summary would silently contradict `data.creators`.
            "creatorSummary": _creator_summary(data["creators"]),
            "parsedDate": date,
            "numChildren": num_children,
        },
        "data": data,
    }


def _creator_summary(creators: list[dict[str, str]] | None) -> str | None:
    """Mimic Zotero's meta.creatorSummary: last name, "and", or "et al."."""
    names = [c.get("lastName") or c.get("name") or "" for c in creators or []]
    names = [n for n in names if n]
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def make_attachment(
    key: str,
    *,
    parent: str = "PARENT01",
    content_type: str = "application/pdf",
    link_mode: str = "imported_file",
    title: str = "Full Text PDF",
) -> dict[str, Any]:
    return {
        "key": key,
        "version": 101,
        "meta": {},
        "data": {
            "key": key,
            "itemType": "attachment",
            "parentItem": parent,
            "title": title,
            "contentType": content_type,
            "linkMode": link_mode,
            "filename": "paper.pdf",
            "tags": [],
            "collections": [],
            "dateAdded": "2024-03-01T10:00:00Z",
        },
    }


def make_note(key: str, *, text: str = "<p>My <b>note</b>.</p>") -> dict[str, Any]:
    return {
        "key": key,
        "version": 102,
        "meta": {},
        "data": {
            "key": key,
            "itemType": "note",
            "note": text,
            "tags": [],
            "dateAdded": "2024-03-01T10:00:00Z",
            "dateModified": "2024-03-02T10:00:00Z",
        },
    }


def make_collection(
    key: str, name: str, *, parent: str | None = None, num_items: int = 0
) -> dict[str, Any]:
    return {
        "key": key,
        "version": 50,
        "meta": {"numItems": num_items, "numCollections": 0},
        "data": {
            "key": key,
            "name": name,
            "parentCollection": parent or False,
        },
    }


class FakeZotero:
    """Records calls, returns canned payloads, and sets response headers."""

    def __init__(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        collections: list[dict[str, Any]] | None = None,
        children: dict[str, list[dict[str, Any]]] | None = None,
        fulltext: dict[str, dict[str, Any]] | None = None,
        tags: list[str] | None = None,
        total_results: int | None = None,
        library_version: int = 500,
        item_types: list[dict[str, str]] | None = None,
        fail_with: Exception | None = None,
        fail_times: int = 0,
    ) -> None:
        self._items = items or []
        self._collections = collections or []
        self._children = children or {}
        self._fulltext = fulltext or {}
        self._tags = tags or []
        self._total = total_results
        self._library_version = library_version
        self._item_types = item_types or [{"itemType": "book", "localized": "Book"}]
        self._fail_with = fail_with
        self._fail_times = fail_times
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.request = FakeResponse()

    # ------------------------------------------------------------------ internals
    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self._fail_with is not None and self._fail_times != 0:
            if self._fail_times > 0:
                self._fail_times -= 1
            raise self._fail_with
        self.request.headers = {"Last-Modified-Version": str(self._library_version)}
        if self._total is not None:
            self.request.headers["Total-Results"] = str(self._total)

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for called, _, kwargs in self.calls if called == name]

    # ---------------------------------------------------------------------- reads
    def top(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("top", **kwargs)
        limit = kwargs.get("limit", 25)
        start = kwargs.get("start", 0)
        return self._items[start : start + limit]

    def items(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("items", **kwargs)
        return self._items

    def item(self, key: str, **kwargs: Any) -> dict[str, Any] | None:
        self._record("item", key, **kwargs)
        for raw in self._items:
            if raw["key"] == key:
                return raw
        from pyzotero.errors import ResourceNotFoundError

        raise ResourceNotFoundError(f"Item {key} not found")

    def children(self, key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("children", key, **kwargs)
        kids = self._children.get(key, [])
        wanted = kwargs.get("itemType")
        if wanted:
            kids = [k for k in kids if k["data"]["itemType"] == wanted]
        return kids

    def collections(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("collections", **kwargs)
        return self._collections

    def collections_sub(self, key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("collections_sub", key, **kwargs)
        return [
            c for c in self._collections if c["data"].get("parentCollection") == key
        ]

    def collection_items(self, key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("collection_items", key, **kwargs)
        return self._items

    def collection_items_top(self, key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("collection_items_top", key, **kwargs)
        limit = kwargs.get("limit", 25)
        return self._items[:limit]

    def tags(self, **kwargs: Any) -> list[str]:
        self._record("tags", **kwargs)
        return self._tags

    def fulltext_item(self, key: str, **kwargs: Any) -> dict[str, Any]:
        self._record("fulltext_item", key, **kwargs)
        if key in self._fulltext:
            return self._fulltext[key]
        from pyzotero.errors import ResourceNotFoundError

        raise ResourceNotFoundError(f"No full text for {key}")

    def num_items(self) -> int:
        self._record("num_items")
        return len(self._items)

    def count_items(self) -> int:
        self._record("count_items")
        return len(self._items)

    def last_modified_version(self, **kwargs: Any) -> int:
        self._record("last_modified_version", **kwargs)
        return self._library_version

    def key_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("key_info", **kwargs)
        return {"userID": 123456, "username": "researcher", "access": {}}

    def item_types(self) -> list[dict[str, str]]:
        self._record("item_types")
        return self._item_types

    def item_type_fields(self, item_type: str) -> list[dict[str, str]]:
        self._record("item_type_fields", item_type)
        return [{"field": "title"}, {"field": "abstractNote"}]
