"""The Zotero MCP server: tools, resources, and the create_server() factory.

Import-time is side-effect free on purpose (PRD 7.2): building a client or reading
config here would make the server impossible to embed in-memory. ``create_server``
constructs everything, and the gateway is opened in the lifespan.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from textwrap import dedent
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from pydantic import Field

from . import projection
from .gateway import (
    ZoteroForbidden,
    ZoteroGateway,
    ZoteroNotFound,
    ZoteroUnavailable,
)
from .models import (
    CitationMatch,
    CollectionTree,
    FullTextResult,
    ItemDetail,
    LibraryInfo,
    Note,
    SearchResults,
    TagInfo,
)
from .settings import ZoteroSettings

logger = logging.getLogger(__name__)

SERVER_VERSION = "0.1.0"

ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
MAX_LIMIT = 100
DEFAULT_LIMIT = 25

#: Every tool only reads the configured Zotero library. All four hints are explicit:
#: some directories (e.g. OpenAI's) reject tools with any hint missing. openWorldHint
#: is True to declare that, in web mode, tools call the external api.zotero.org.
READ_ONLY_TOOL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

SearchMode = Literal["metadata", "fulltext"]
SortField = Literal[
    "relevance", "dateAdded", "dateModified", "title", "creator", "date", "publisher"
]

INSTRUCTIONS = dedent("""\
    Search and read a researcher's own Zotero library: a curated, human-selected
    corpus of references, with tags, collections, notes, and the indexed full text of
    attached PDFs.

    Start with `get_library_info` to see how large the library is and whether writes
    are permitted. Use `search_items` as the main entry point — `mode="metadata"`
    searches titles, creators, and years; `mode="fulltext"` also searches the text of
    attached documents. Narrow with `collection_key` (the researcher's own
    organization) or `tag` before raising `limit`.

    Every item carries an 8-character `key`. Pass that key to `get_item`,
    `get_item_fulltext`, and `get_item_notes`. Notes are the researcher's own
    commentary and are often more useful than the published abstract.

    Results are candidates, not verdicts. When several items plausibly match a
    reference — a pre-print and its published version, say — all are returned with
    the basis for the match, and choosing between them is the caller's job. Cite only
    items that appear in results; never invent a reference.
""")


# --------------------------------------------------------------------------- errors


def _tool_error(exc: BaseException, *, context: str) -> ToolError:
    """Translate a gateway error into something the model can act on (guide 3.5)."""
    if isinstance(exc, ZoteroNotFound):
        return ToolError(
            f"{context}: not found in this library. "
            "Use `search_items` to find valid item keys."
        )
    if isinstance(exc, ZoteroForbidden):
        return ToolError(
            f"{context}: Zotero rejected the credentials. Verify ZOTERO_API_KEY and "
            "that it grants access to this library."
        )
    if isinstance(exc, ZoteroUnavailable):
        return ToolError(f"{context}: Zotero API unavailable ({exc}). Retry shortly.")
    return ToolError(f"{context}: unexpected failure.")


def _require_item_key(value: str, *, field_name: str = "item_key") -> str:
    key = (value or "").strip().upper()
    if not ITEM_KEY_RE.match(key):
        raise ToolError(
            f"`{field_name}` must be an 8-character Zotero key (letters and digits), "
            f"got {value!r}. Keys come from `search_items` results."
        )
    return key


# ---------------------------------------------------------------------- the factory


def create_server(
    settings: ZoteroSettings | None = None,
    *,
    gateway: ZoteroGateway | None = None,
) -> FastMCP:
    """Build a configured Zotero MCP server.

    Args:
        settings: Configuration. Defaults to environment-derived settings, but a host
            process embedding this server may pass an object instead and never set an
            environment variable.
        gateway: Pre-built gateway, for tests and for hosts that manage their own
            pyzotero clients.

    The returned server is not connected to anything yet: the gateway opens in the
    lifespan, so this is safe to call at import time in a host process.
    """
    cfg = settings or ZoteroSettings()
    cfg.validate_for_startup()
    gw = gateway or ZoteroGateway(cfg)

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        info: dict[str, Any] = {}
        try:
            info = await gw.verify_credentials()
        except Exception as exc:  # noqa: BLE001 - startup diagnostics only
            # A bad key should surface on the first call with a clear message rather
            # than crashing an editor's MCP launch, so this warns instead of raising.
            logger.warning("could not verify Zotero credentials at startup: %s", exc)
        logger.info(
            "zotero-mcp ready (library=%s/%s mode=%s writes=%s)",
            cfg.library_type,
            cfg.library_id,
            "local" if cfg.local else "web",
            cfg.allow_writes,
        )
        try:
            yield {"key_info": info}
        finally:
            await gw.aclose()

    mcp = FastMCP(
        name="Zotero",
        instructions=INSTRUCTIONS,
        version=SERVER_VERSION,
        lifespan=lifespan,
        mask_error_details=True,
        auth=_build_auth(cfg),
    )

    _register_read_tools(mcp, gw)
    _register_resources(mcp, gw)

    if cfg.allow_writes:
        # Write tools are registered only when enabled, so a read-only deployment
        # does not even advertise them (see README: deviation from PRD 5.5, which
        # specified enabled=False).
        logger.warning("write tools are ENABLED for this Zotero library")

    return mcp


def _build_auth(cfg: ZoteroSettings):
    """Static bearer-token auth for HTTP. None for stdio and in-memory."""
    if cfg.transport != "http" or not cfg.auth_token:
        return None
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    return StaticTokenVerifier(
        tokens={cfg.auth_token: {"client_id": "zotero-mcp", "scopes": ["zotero.read"]}}
    )


# ----------------------------------------------------------------------- read tools


def _register_read_tools(mcp: FastMCP, gw: ZoteroGateway) -> None:
    cfg = gw.settings

    async def _collection_names() -> dict[str, str]:
        """Cached key -> name map, so summaries show names not keys."""

        async def load() -> dict[str, str]:
            raw = await gw.call(lambda z: z.collections(limit=MAX_LIMIT), "collections")
            return projection.collection_name_map(raw or [])

        try:
            return await gw.cached("collection_names", load)
        except Exception:  # cosmetic; never fail a search over it
            logger.debug("collection name map unavailable", exc_info=True)
            return {}

    def _results(
        raw_items: list[dict[str, Any]],
        *,
        total: int | None,
        start: int,
        limit: int,
        names: dict[str, str],
        narrowing_hint: str,
    ) -> SearchResults:
        items = [
            projection.item_summary(raw, collection_names=names)
            for raw in raw_items or []
        ]
        returned = len(items)
        end = start + returned
        truncated = bool(total is not None and end < total)
        return SearchResults(
            items=items,
            returned=returned,
            total_matched=total,
            truncated=truncated,
            next_start=end if truncated else None,
            hint=(
                f"{total} items matched; {returned} returned. "
                f"Pass start={end} for the next page, or {narrowing_hint}."
                if truncated
                else None
            ),
        )

    # ---------------------------------------------------------------- orientation
    @mcp.tool(
        tags={"zotero", "library"},
        annotations=READ_ONLY_TOOL,
    )
    async def get_library_info() -> LibraryInfo:
        """Summarize the connected Zotero library: size, mode, and permissions.

        Cheap orientation call. Use it first to learn how big the library is and
        whether writes are possible before planning a search strategy.
        """
        try:
            num_items = await gw.call(lambda z: z.num_items(), "num_items")
        except Exception as exc:
            raise _tool_error(exc, context="Reading library info") from exc

        try:
            collections = await gw.cached(
                "collections_raw",
                lambda: gw.call(
                    lambda z: z.collections(limit=MAX_LIMIT), "collections"
                ),
            )
            num_collections = len(collections or [])
        except Exception:  # noqa: BLE001 - non-essential detail
            num_collections = None

        key_info: dict[str, Any] = {}
        try:
            key_info = await gw.verify_credentials()
        except Exception:  # noqa: BLE001 - non-essential detail
            key_info = {}

        return LibraryInfo(
            library_type=cfg.library_type,
            library_id=str(cfg.library_id or ""),
            mode="local" if cfg.local else "web",
            num_items=num_items,
            num_collections=num_collections,
            last_modified_version=await gw.library_version(),
            writes_enabled=cfg.allow_writes,
            fulltext_max_chars=cfg.fulltext_max_chars,
            default_style=cfg.default_style,
            username=key_info.get("username") if isinstance(key_info, dict) else None,
        )

    # --------------------------------------------------------------------- search
    @mcp.tool(
        tags={"zotero", "search"},
        annotations=READ_ONLY_TOOL,
    )
    async def search_items(
        query: Annotated[
            str,
            Field(
                description=(
                    "Search terms. Matches titles, creators, and years; also "
                    "attachment text when mode='fulltext'."
                )
            ),
        ],
        mode: Annotated[
            SearchMode,
            Field(description="'metadata' (fast) or 'fulltext' (searches PDF text)"),
        ] = "metadata",
        item_type: Annotated[
            str | None,
            Field(
                description=(
                    "Restrict to a Zotero item type, e.g. 'journalArticle' or "
                    "'book'. Prefix with '-' to exclude."
                )
            ),
        ] = None,
        tag: Annotated[
            str | None,
            Field(
                description=(
                    "Tag filter. 'a || b' means either; '-a' excludes. See "
                    "`list_tags` for what exists."
                )
            ),
        ] = None,
        collection_key: Annotated[
            str | None,
            Field(description="Restrict to one collection (8-character key)"),
        ] = None,
        sort: SortField = "relevance",
        direction: Literal["asc", "desc"] = "desc",
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        start: Annotated[int, Field(ge=0)] = 0,
        ctx: Context | None = None,
    ) -> SearchResults:
        """Search the library and return compact item summaries.

        The primary entry point. Returns top-level items (not attachments), each with
        an 8-character `key` for follow-up calls. Prefer narrowing with `tag` or
        `collection_key` over raising `limit`.
        """
        if not query.strip():
            raise ToolError(
                "`query` is empty. Pass search terms, or use `list_recent_items` to "
                "browse without a query."
            )

        params: dict[str, Any] = {
            "q": query.strip(),
            "qmode": "everything" if mode == "fulltext" else "titleCreatorYear",
            "limit": limit,
            "start": start,
        }
        if item_type:
            params["itemType"] = item_type
        if tag:
            params["tag"] = tag
        if sort != "relevance":
            params["sort"] = sort
            params["direction"] = direction

        if collection_key:
            key = _require_item_key(collection_key, field_name="collection_key")
            op = lambda z: z.collection_items_top(key, **params)
            narrowing = "add `tag` or more specific terms"
        else:
            op = lambda z: z.top(**params)
            narrowing = "add `collection_key`, `tag`, or `item_type`"

        if ctx:
            await ctx.debug(f"searching ({mode}): {query!r}")

        try:
            fetched = await gw.fetch(op, name="search_items")
        except Exception as exc:
            raise _tool_error(exc, context=f"Searching for {query!r}") from exc

        names = await _collection_names()
        return _results(
            fetched.value,
            total=fetched.total_results,
            start=start,
            limit=limit,
            names=names,
            narrowing_hint=narrowing,
        )

    @mcp.tool(
        tags={"zotero", "search"},
        annotations=READ_ONLY_TOOL,
    )
    async def list_recent_items(
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        since_days: Annotated[
            int | None,
            Field(
                ge=1,
                description="Only items added in the last N days (filtered locally)",
            ),
        ] = None,
        start: Annotated[int, Field(ge=0)] = 0,
    ) -> SearchResults:
        """List the most recently added items, newest first.

        Answers "what have I been reading lately?" `since_days` filters the returned
        page by `dateAdded`; because Zotero has no server-side date filter, a narrow
        window may return fewer items than `limit`.
        """
        params = {
            "limit": limit,
            "start": start,
            "sort": "dateAdded",
            "direction": "desc",
        }
        try:
            fetched = await gw.fetch(lambda z: z.top(**params), name="list_recent")
        except Exception as exc:
            raise _tool_error(exc, context="Listing recent items") from exc

        names = await _collection_names()
        results = _results(
            fetched.value,
            total=fetched.total_results,
            start=start,
            limit=limit,
            names=names,
            narrowing_hint="use `search_items` to target a topic",
        )

        if since_days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=since_days)).date()
            kept = [i for i in results.items if i.date_added and i.date_added >= cutoff]
            dropped = results.returned - len(kept)
            results.items = kept
            results.returned = len(kept)
            if dropped:
                results.hint = (
                    f"{dropped} item(s) on this page were older than {since_days} "
                    "days and were filtered out locally."
                )
        return results

    @mcp.tool(
        tags={"zotero", "search"},
        annotations=READ_ONLY_TOOL,
    )
    async def find_item_by_identifier(
        identifier: Annotated[
            str,
            Field(
                description=(
                    "A DOI, ISBN, arXiv ID, or 8-character Zotero key. Titles work "
                    "too, but `search_items` is better for those."
                )
            ),
        ],
    ) -> CitationMatch:
        """Check whether the library already contains a specific reference.

        Returns *candidates* with the basis for each match, not a single verdict: a
        pre-print and its published version are both returned when both exist. The
        caller decides which — if either — is the intended item.
        """
        raw_query = (identifier or "").strip()
        if not raw_query:
            raise ToolError("`identifier` is empty.")

        names = await _collection_names()

        # 1. An exact Zotero key is unambiguous.
        if ITEM_KEY_RE.match(raw_query.upper()):
            key = raw_query.upper()
            try:
                raw = await gw.call(lambda z: z.item(key), "item")
            except ZoteroNotFound:
                raw = None
            except Exception as exc:
                raise _tool_error(exc, context=f"Looking up {key}") from exc
            if raw:
                return CitationMatch(
                    query=raw_query,
                    matched_on="key",
                    confidence=1.0,
                    candidates=[projection.item_summary(raw, collection_names=names)],
                )

        # 2. Otherwise search, then report on what the match was based on.
        try:
            fetched = await gw.fetch(
                lambda z: z.top(q=raw_query, qmode="everything", limit=25),
                name="find_by_identifier",
            )
        except Exception as exc:
            raise _tool_error(exc, context=f"Searching for {raw_query!r}") from exc

        candidates = [
            projection.item_summary(raw, collection_names=names)
            for raw in fetched.value or []
        ]
        if not candidates:
            return CitationMatch(
                query=raw_query,
                matched_on="none",
                confidence=0.0,
                note=(
                    "No candidates in this library. The reference may be absent, or "
                    "indexed under a different title."
                ),
            )

        wanted_doi = projection.normalize_doi(raw_query)
        if wanted_doi:
            doi_hits = [c for c in candidates if c.doi and c.doi == wanted_doi]
            if doi_hits:
                return CitationMatch(
                    query=raw_query,
                    matched_on="doi",
                    confidence=1.0,
                    candidates=doi_hits,
                    note=(
                        f"{len(doi_hits)} items share this DOI."
                        if len(doi_hits) > 1
                        else None
                    ),
                )

        lowered = raw_query.lower()
        title_hits = [c for c in candidates if c.title.lower() == lowered]
        if title_hits:
            return CitationMatch(
                query=raw_query,
                matched_on="title",
                confidence=0.8,
                candidates=title_hits,
                note=(
                    "Multiple items share this title (e.g. pre-print and published "
                    "version); the caller should choose."
                    if len(title_hits) > 1
                    else None
                ),
            )

        return CitationMatch(
            query=raw_query,
            matched_on="identifier",
            confidence=0.4,
            candidates=candidates[:10],
            note=(
                "No exact DOI or title match; these are search candidates only. "
                "Verify before citing."
            ),
        )

    # ----------------------------------------------------------------- read items
    @mcp.tool(
        tags={"zotero", "items"},
        annotations=READ_ONLY_TOOL,
    )
    async def get_item(
        item_key: Annotated[str, Field(description="8-character Zotero item key")],
        include_children: Annotated[
            bool,
            Field(description="Also list attachments and notes (one extra request)"),
        ] = False,
    ) -> ItemDetail:
        """Get full metadata for one item, including its abstract.

        With `include_children=True` the response also lists attachments and notes,
        and `has_fulltext` becomes definitive rather than null.
        """
        key = _require_item_key(item_key)
        try:
            raw = await gw.call(lambda z: z.item(key), "item")
        except Exception as exc:
            raise _tool_error(exc, context=f"Reading item {key}") from exc
        if not raw:
            raise ToolError(f"No item with key {key} in this library.")

        children = None
        if include_children:
            try:
                raw_children = await gw.call(lambda z: z.children(key), "children")
                children = [projection.child_summary(c) for c in raw_children or []]
            except Exception:  # metadata is still worth returning
                logger.debug("children lookup failed for %s", key, exc_info=True)

        names = await _collection_names()
        return projection.item_detail(raw, collection_names=names, children=children)

    @mcp.tool(
        tags={"zotero", "items"},
        annotations=READ_ONLY_TOOL,
    )
    async def get_item_children(
        item_key: Annotated[str, Field(description="8-character Zotero item key")],
    ) -> list[dict[str, Any]]:
        """List an item's attachments and notes.

        Use it to see whether an item has an indexable PDF (`may_have_fulltext`)
        before calling `get_item_fulltext`.
        """
        key = _require_item_key(item_key)
        try:
            raw = await gw.call(lambda z: z.children(key), "children")
        except Exception as exc:
            raise _tool_error(exc, context=f"Listing children of {key}") from exc
        return [projection.child_summary(c).model_dump(mode="json") for c in raw or []]

    @mcp.tool(
        tags={"zotero", "items", "notes"},
        annotations=READ_ONLY_TOOL,
    )
    async def get_item_notes(
        item_key: Annotated[str, Field(description="8-character Zotero item key")],
    ) -> list[Note]:
        """Get the researcher's own notes attached to an item.

        Notes record why this item was kept and what the researcher concluded — often
        more informative than the published abstract. Returns an empty list when the
        item has no notes.
        """
        key = _require_item_key(item_key)
        try:
            raw = await gw.call(
                lambda z: z.children(key, itemType="note"), "children_notes"
            )
        except Exception as exc:
            raise _tool_error(exc, context=f"Reading notes for {key}") from exc
        return [projection.note(c) for c in raw or []]

    @mcp.tool(
        tags={"zotero", "fulltext"},
        annotations=READ_ONLY_TOOL,
    )
    async def get_item_fulltext(
        item_key: Annotated[
            str,
            Field(
                description=(
                    "8-character key of the item or its attachment. Parent items are "
                    "resolved to their first indexable attachment automatically."
                )
            ),
        ],
        max_chars: Annotated[
            int | None,
            Field(
                ge=1_000,
                description=(
                    "Truncate after this many characters. Defaults to the server's "
                    "configured ceiling."
                ),
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> FullTextResult:
        """Read the indexed full text of an item's attachment.

        Returns text Zotero has already extracted — no OCR happens here. Long
        documents are truncated at `max_chars` and `truncated` says so; nothing is
        dropped silently. Fails cleanly when the item has no indexable attachment.
        """
        key = _require_item_key(item_key)
        ceiling = max_chars or cfg.fulltext_max_chars

        async def read_text(target: str) -> dict[str, Any] | None:
            try:
                return await gw.call(lambda z: z.fulltext_item(target), "fulltext_item")
            except ZoteroNotFound:
                return None
            except Exception as exc:
                raise _tool_error(
                    exc, context=f"Reading full text of {target}"
                ) from exc

        payload = await read_text(key)
        attachment_key: str | None = key if payload else None

        if payload is None:
            # The key was probably a parent item; find an indexable attachment.
            try:
                raw_children = await gw.call(lambda z: z.children(key), "children")
            except Exception as exc:
                raise _tool_error(exc, context=f"Reading item {key}") from exc

            children = [projection.child_summary(c) for c in raw_children or []]
            indexable = [c for c in children if c.may_have_fulltext]
            if not indexable:
                attachments = [c for c in children if c.child_type == "attachment"]
                raise ToolError(
                    f"No indexed full text for {key} "
                    f"({len(attachments)} attachment(s), none indexable). "
                    "Use `get_item` for metadata or `get_item_notes` for commentary."
                )
            for child in indexable:
                if ctx:
                    await ctx.debug(f"trying attachment {child.key}")
                payload = await read_text(child.key)
                if payload:
                    attachment_key = child.key
                    break

        if not payload:
            raise ToolError(
                f"No indexed full text for {key}. Zotero has the attachment but has "
                "not indexed its text; open the item in Zotero to trigger indexing."
            )

        text = payload.get("content") or ""
        total = len(text)
        truncated = total > ceiling
        return FullTextResult(
            item_key=key,
            attachment_key=attachment_key,
            text=text[:ceiling] if truncated else text,
            total_chars=total,
            returned_chars=min(total, ceiling),
            truncated=truncated,
            indexed_pages=payload.get("indexedPages"),
            total_pages=payload.get("totalPages"),
        )

    # ------------------------------------------------------------------ structure
    @mcp.tool(
        tags={"zotero", "collections"},
        annotations=READ_ONLY_TOOL,
    )
    async def list_collections(
        parent_key: Annotated[
            str | None,
            Field(
                description="Only this collection's subtree; omit for the whole tree"
            ),
        ] = None,
        include_counts: Annotated[
            bool, Field(description="Include per-collection item counts")
        ] = True,
    ) -> CollectionTree:
        """List collections as a nested tree.

        This is the researcher's own taxonomy — the fastest way to understand how the
        library is organized. Pass a collection's `key` to `search_items` or
        `list_collection_items` to scope a query to it.
        """
        if parent_key:
            key = _require_item_key(parent_key, field_name="parent_key")
            try:
                raw = await gw.call(
                    lambda z: z.collections_sub(key, limit=MAX_LIMIT), "collections_sub"
                )
            except Exception as exc:
                raise _tool_error(
                    exc, context=f"Listing subcollections of {key}"
                ) from exc
        else:
            try:
                raw = await gw.cached(
                    "collections_raw",
                    lambda: gw.call(
                        lambda z: z.collections(limit=MAX_LIMIT), "collections"
                    ),
                )
            except Exception as exc:
                raise _tool_error(exc, context="Listing collections") from exc

        nodes, total = projection.collection_tree(
            raw or [], include_counts=include_counts
        )
        return CollectionTree(collections=nodes, total=total)

    @mcp.tool(
        tags={"zotero", "collections"},
        annotations=READ_ONLY_TOOL,
    )
    async def list_collection_items(
        collection_key: Annotated[
            str, Field(description="8-character collection key from `list_collections`")
        ],
        recursive: Annotated[
            bool,
            Field(description="Include items in subcollections as well"),
        ] = False,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 50,
        start: Annotated[int, Field(ge=0)] = 0,
    ) -> SearchResults:
        """List the items filed in one collection.

        Browsing rather than searching: use it when the researcher's own grouping is
        the right unit of analysis.
        """
        key = _require_item_key(collection_key, field_name="collection_key")
        params: dict[str, Any] = {"limit": limit, "start": start}
        op = (
            (lambda z: z.collection_items(key, **params))
            if recursive
            else (lambda z: z.collection_items_top(key, **params))
        )
        try:
            fetched = await gw.fetch(op, name="collection_items")
        except Exception as exc:
            raise _tool_error(exc, context=f"Listing items in {key}") from exc

        names = await _collection_names()
        return _results(
            fetched.value,
            total=fetched.total_results,
            start=start,
            limit=limit,
            names=names,
            narrowing_hint="use `search_items` with collection_key to filter",
        )

    @mcp.tool(
        tags={"zotero", "tags"},
        annotations=READ_ONLY_TOOL,
    )
    async def list_tags(
        prefix: Annotated[
            str | None, Field(description="Only tags starting with this text")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = MAX_LIMIT,
    ) -> list[TagInfo]:
        """List tags used in the library.

        Tags are the researcher's own vocabulary. Read them before guessing a `tag`
        filter for `search_items`.
        """
        try:
            raw = await gw.cached(
                "tags_raw",
                lambda: gw.call(lambda z: z.tags(limit=MAX_LIMIT), "tags"),
            )
        except Exception as exc:
            raise _tool_error(exc, context="Listing tags") from exc

        tags = [t for t in raw or [] if isinstance(t, str)]
        if prefix:
            lowered = prefix.lower()
            tags = [t for t in tags if t.lower().startswith(lowered)]
        return [TagInfo(tag=t) for t in sorted(tags)[:limit]]


# ------------------------------------------------------------------------ resources


def _register_resources(mcp: FastMCP, gw: ZoteroGateway) -> None:
    cfg = gw.settings

    def _resource_key(value: str) -> str:
        key = (value or "").strip().upper()
        if not ITEM_KEY_RE.match(key):
            raise ResourceError("Unknown key: expected an 8-character Zotero key.")
        return key

    @mcp.resource(
        "zotero://library/info",
        name="ZoteroLibraryInfo",
        mime_type="application/json",
        tags={"zotero"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def library_info_resource() -> dict[str, Any]:
        """Summary of the connected Zotero library."""
        try:
            num_items = await gw.call(lambda z: z.num_items(), "num_items")
        except Exception as exc:
            raise ResourceError(f"Zotero unavailable: {exc}") from exc
        return {
            "library_type": cfg.library_type,
            "library_id": str(cfg.library_id or ""),
            "mode": "local" if cfg.local else "web",
            "num_items": num_items,
            "last_modified_version": await gw.library_version(),
            "writes_enabled": cfg.allow_writes,
        }

    @mcp.resource(
        "zotero://collections",
        name="ZoteroCollections",
        mime_type="application/json",
        tags={"zotero"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def collections_resource() -> dict[str, Any]:
        """The library's collection tree."""
        try:
            raw = await gw.cached(
                "collections_raw",
                lambda: gw.call(
                    lambda z: z.collections(limit=MAX_LIMIT), "collections"
                ),
            )
        except Exception as exc:
            raise ResourceError(f"Zotero unavailable: {exc}") from exc
        nodes, total = projection.collection_tree(raw or [])
        return CollectionTree(collections=nodes, total=total).model_dump(mode="json")

    @mcp.resource(
        "zotero://items/{item_key}",
        name="ZoteroItem",
        mime_type="application/json",
        tags={"zotero"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def item_resource(item_key: str) -> dict[str, Any]:
        """Metadata for a single Zotero item."""
        key = _resource_key(item_key)
        try:
            raw = await gw.call(lambda z: z.item(key), "item")
        except ZoteroNotFound as exc:
            raise ResourceError(f"No item with key {key}.") from exc
        except Exception as exc:
            raise ResourceError(f"Zotero unavailable: {exc}") from exc
        if not raw:
            raise ResourceError(f"No item with key {key}.")
        return projection.item_detail(raw).model_dump(mode="json")

    @mcp.resource(
        "zotero://items/{item_key}/fulltext",
        name="ZoteroItemFullText",
        mime_type="text/plain",
        tags={"zotero", "fulltext"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def item_fulltext_resource(item_key: str) -> str:
        """Indexed full text of an item's attachment, truncated to the configured ceiling."""
        key = _resource_key(item_key)
        try:
            payload = await gw.call(lambda z: z.fulltext_item(key), "fulltext_item")
        except ZoteroNotFound as exc:
            raise ResourceError(
                f"No indexed full text for {key}. It may be a parent item — use the "
                "get_item_fulltext tool, which resolves attachments."
            ) from exc
        except Exception as exc:
            raise ResourceError(f"Zotero unavailable: {exc}") from exc

        text = (payload or {}).get("content") or ""
        if not text:
            raise ResourceError(f"No indexed full text for {key}.")
        return text[: cfg.fulltext_max_chars]

    @mcp.resource(
        "zotero://collections/{collection_key}/items",
        name="ZoteroCollectionItems",
        mime_type="application/json",
        tags={"zotero"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def collection_items_resource(collection_key: str) -> dict[str, Any]:
        """Top-level items filed in one collection."""
        key = _resource_key(collection_key)
        try:
            fetched = await gw.fetch(
                lambda z: z.collection_items_top(key, limit=50), name="collection_items"
            )
        except ZoteroNotFound as exc:
            raise ResourceError(f"No collection with key {key}.") from exc
        except Exception as exc:
            raise ResourceError(f"Zotero unavailable: {exc}") from exc

        items = [projection.item_summary(raw) for raw in fetched.value or []]
        return SearchResults(
            items=items,
            returned=len(items),
            total_matched=fetched.total_results,
            truncated=bool(
                fetched.total_results is not None and len(items) < fetched.total_results
            ),
        ).model_dump(mode="json")

    @mcp.resource(
        "zotero://schema/item-types",
        name="ZoteroItemTypes",
        mime_type="application/json",
        tags={"zotero", "schema"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def item_types_resource() -> list[dict[str, Any]]:
        """Valid Zotero item types."""
        try:
            raw = await gw.cached(
                "item_types", lambda: gw.call(lambda z: z.item_types(), "item_types")
            )
        except Exception as exc:
            raise ResourceError(f"Zotero unavailable: {exc}") from exc
        return list(raw or [])

    @mcp.resource(
        "zotero://schema/item-types/{item_type}/fields",
        name="ZoteroItemTypeFields",
        mime_type="application/json",
        tags={"zotero", "schema"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def item_type_fields_resource(item_type: str) -> list[dict[str, Any]]:
        """Valid fields for one Zotero item type."""
        name = (item_type or "").strip()
        if not name.isalpha():
            raise ResourceError(f"Unknown item type: {item_type!r}")
        try:
            raw = await gw.cached(
                f"item_type_fields:{name}",
                lambda: gw.call(lambda z: z.item_type_fields(name), "item_type_fields"),
            )
        except ZoteroNotFound as exc:
            raise ResourceError(f"Unknown item type: {name!r}") from exc
        except Exception as exc:
            raise ResourceError(f"Zotero unavailable: {exc}") from exc
        return list(raw or [])


#: Module-level, environment-configured server for stdio launch and `fastmcp run`.
#: Constructed lazily by __main__ so that importing this module stays side-effect free.
def build_default_server() -> FastMCP:
    """Create a server from environment configuration."""
    return create_server()
