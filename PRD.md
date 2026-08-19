# An MCP server for the Zotero bibliography service

**Status:** Draft · **Owner:** Jorge Londoño · **Last updated:** 2026-08-17

## References

[Pyzotero Documentation](https://pyzotero.readthedocs.io/en/latest/)
[Zotero Web API v3 — Basics](https://www.zotero.org/support/dev/web_api/v3/basics)
[Zotero Web API v3 — Write Requests](https://www.zotero.org/support/dev/web_api/v3/write_requests)
[FastMCP documentation](https://gofastmcp.com/) — this server must conform to FastMCP conventions

[Free Github MCP](https://github.com/54yyyu/zotero-mcp)  

---

## 1. Summary

`zotero-mcp` is a FastMCP server that exposes a Zotero library — personal or group — to AI agents
as a set of tools, resources, and prompts. The agent gains the ability to **search a researcher's
own curated corpus**, read the full text of attached PDFs, resolve citations, and generate
formatted bibliographies, without the researcher pasting references by hand.

The bet: a Zotero library is a high-signal, human-curated knowledge base. Generic web search
returns what exists; a Zotero library returns what *this researcher already decided was worth
keeping*. That makes it a materially better retrieval target for literature review, citation
checking, and writing support.

---

## 2. Problem

Researchers using AI assistants for literature work hit the same wall repeatedly:

1. **The corpus is invisible to the agent.** Hundreds of curated, tagged, annotated references sit
   in Zotero, and the agent cannot see any of them. Every session starts from zero context.
2. **Manual transfer is lossy and slow.** Copy-pasting titles and abstracts drops tags, collection
   structure, notes, and the attached full text — exactly the parts that carry the researcher's
   own judgment.
3. **Citations get fabricated.** Asked for references, models invent plausible ones. A tool that
   returns *only* real items from a real library, with keys and DOIs, converts a hallucination
   problem into a retrieval problem.
4. **Formatting is manual.** Producing a correctly styled bibliography for a subset of items means
   leaving the conversation and using the Zotero UI.

---

## 3. Goals and non-goals

### Goals

| # | Goal | Success signal |
|---|---|---|
| G1 | Agents can search a Zotero library by metadata and by attachment full text | An agent answers "what do I have on transformer interpretability?" from the library alone |
| G2 | Agents can read complete item metadata and PDF full text | Agent summarizes a paper it was never given, from a library key |
| G3 | Agents can navigate the researcher's own organization (collections, tags) | Agent scopes a query to one collection |
| G4 | Agents produce correctly formatted citations and bibliographies in any CSL style | Zero-edit bibliography pasted into a manuscript |
| G5 | Every returned reference is real and verifiable | Every result carries a Zotero key and, where available, DOI/URL |
| G6 | Read-only by default; writes are explicitly opted into | Default install cannot mutate the library |
| G7 | Responses are token-efficient | A 25-item search costs a small fraction of raw Zotero JSON |

### Non-goals (v1)

- **Not a Zotero client.** No UI, no sync engine, no offline replica.
- **Not a PDF processing pipeline.** We read the full text Zotero has already indexed; we do not
  OCR, chunk, or embed.
- **No semantic/vector search.** Zotero's own search only. Revisit in v2 (§12).
- **No multi-user service.** One server process serves one credential, whichever transport it runs
  on. HTTP mode is a shared *endpoint*, not a multi-tenant one — every caller reaches the same
  library. No tenant model.
- **No file/attachment upload** in v1.
- **No OAuth flow.** API key only (§8).
- **No group administration** (membership, permissions).

---

## 4. Users

| Persona | Need | Primary tools |
|---|---|---|
| **Researcher writing a paper** (primary) | Find own prior reading, cite correctly, generate bibliography | `search_items`, `format_bibliography` |
| **Researcher doing a literature review** | Survey a topic across own corpus, find gaps, read full text | `search_items`, `get_item_fulltext`, `list_collection_items` |
| **Research group member** | Query a shared group library | all read tools, `library_type="group"` |
| **Library curator** (writes opt-in) | Tag, file, and clean up items in bulk | `add_item_tags`, `add_items_to_collection` |

Assumed environment: a PydanticAI or Claude-based agent connecting over STDIO on the
researcher's own machine.

---

## 5. Scope: the tool surface

Design constraints applied throughout: task-shaped tools rather than a
1:1 mapping of pyzotero methods; every tool annotated `readOnlyHint=True` unless it writes;
structured, bounded output; explicit units and ID formats.

### 5.1 Search and discovery

| Tool | Signature (abbreviated) | Notes |
|---|---|---|
| `search_items` | `(query, mode="metadata"\|"fulltext", item_type=None, tag=None, collection_key=None, sort="relevance", limit=25) -> SearchResults` | **The primary entry point.** `mode="fulltext"` maps to `qmode=everything`, searching indexed attachment text. Returns trimmed item summaries. |
| `list_recent_items` | `(limit=25, since_days=None) -> SearchResults` | `sort=dateAdded, direction=desc`. Answers "what did I add lately?" |
| `find_item_by_identifier` | `(identifier) -> CitationMatch` | DOI, ISBN, arXiv ID, or Zotero key. Answers "do I already have this?" Returns candidates with match basis, not a single verdict (§5.7). |

`SearchResults` is `{ items: [ItemSummary], total_matched: int, returned: int, truncated: bool }`.
`truncated` is set from the `Total-Results` header — never silently drop results (see §7.3).

### 5.2 Reading items

| Tool | Signature | Notes |
|---|---|---|
| `get_item` | `(item_key, include_children=False) -> ItemDetail` | Full metadata; optionally notes and attachment stubs. |
| `get_item_fulltext` | `(item_key, max_chars=None) -> FullTextResult` | Zotero's indexed attachment text. Resolves parent → attachment automatically. Reports `truncated` and `total_chars`. `max_chars` defaults to `ZOTERO_FULLTEXT_MAX_CHARS` (100,000). |
| `get_item_notes` | `(item_key) -> list[Note]` | The researcher's own annotations — high-value, distinct from published abstract. |
| `get_item_children` | `(item_key) -> list[ChildSummary]` | Attachments and notes with types and content availability. |

**`get_item_fulltext` is the highest-value tool in the server** and the one with the sharpest
design risk: papers exceed any sane context budget. v1 truncates at `max_chars` with an explicit
flag. v2 adds `section` / offset windowing (§12).

The ceiling is **configurable, defaulting to 100,000 characters** (~25–30k tokens — a long
research paper in full). Precedence: the tool's `max_chars` argument, else
`ZOTERO_FULLTEXT_MAX_CHARS`, else 100,000. Rationale for a generous default: truncating a paper
mid-argument produces confidently wrong summaries, which is worse than a large response, and
callers with tighter budgets can pass `max_chars` per call. Deployments serving small-context
models should lower the env default.

### 5.3 Library structure

| Tool | Signature | Notes |
|---|---|---|
| `list_collections` | `(parent_key=None, include_counts=True) -> CollectionTree` | Nested; the researcher's own taxonomy. |
| `list_collection_items` | `(collection_key, recursive=False, limit=50) -> SearchResults` | Scoped browse. |
| `list_tags` | `(limit=200, prefix=None) -> list[TagInfo]` | With item counts, so the agent can pick discriminating tags. |
| `get_library_info` | `() -> LibraryInfo` | Item/collection counts, library type, `last_modified_version`, write permission. **Orientation tool** — cheap first call. |

### 5.4 Citation and export

| Tool | Signature | Notes |
|---|---|---|
| `format_citation` | `(item_keys, style="chicago-note-bibliography", locale="en-US") -> list[str]` | Server-side CSL rendering. Max 150 keys (API `format=bib` limit). |
| `format_bibliography` | `(item_keys, style=..., format="html"\|"text") -> str` | Single assembled bibliography. |
| `export_items` | `(item_keys, format="bibtex"\|"ris"\|"csljson"\|"biblatex"\|"csv") -> str` | For handoff into LaTeX/Word pipelines. |

Style names are CSL identifiers. `list_citation_styles` is **out of scope** for v1 — the agent
passes a style name and gets a `ToolError` naming valid alternatives if it is wrong.

### 5.5 Write tools (opt-in, `ZOTERO_ALLOW_WRITES=true`)

Disabled by default via `@mcp.tool(enabled=False)`, enabled at startup only when the flag is set
**and** `key_info()` confirms write permission.

| Tool | Signature | Annotation |
|---|---|---|
| `create_item` | `(item_type, fields, collection_key=None, tags=None) -> CreatedItem` | `destructiveHint=False` |
| `update_item_fields` | `(item_key, fields) -> UpdatedItem` | PATCH semantics; never PUT (§7.5) |
| `add_item_tags` | `(item_key, tags) -> UpdatedItem` | additive |
| `add_items_to_collection` | `(collection_key, item_keys) -> BatchResult` | ≤50 keys |
| `create_note` | `(parent_item_key, note_html) -> CreatedItem` | agent's findings back into the library |

**Explicitly excluded from v1, at any flag setting:** `delete_item`, `delete_collection`,
`delete_tags`, trash emptying. Destructive operations on a researcher's bibliography — often years
of irreplaceable curation — are not worth the risk of a model misfiring. Deletion stays in the
Zotero UI.

### 5.6 Resources

Resources cover stable, addressable, browse-oriented data (per guide §1):

| URI | Content |
|---|---|
| `zotero://library/info` | Library summary; same payload as `get_library_info` |
| `zotero://collections` | Full collection tree |
| `zotero://items/{item_key}` | Item metadata, JSON |
| `zotero://items/{item_key}/fulltext` | Indexed full text, `text/plain` |
| `zotero://collections/{collection_key}/items` | Items in a collection |
| `zotero://schema/item-types` | Valid item types (from `zot.item_types()`) |
| `zotero://schema/item-types/{item_type}/fields` | Valid fields per type — **required** for correct `create_item` calls |

Item keys are 8-character alphanumeric. Templates must validate this shape and raise
`ResourceError("Unknown item key")` rather than passing junk upstream.

### 5.7 Prompts

| Prompt | Arguments | Encoded procedure |
|---|---|---|
| `literature_review` | `topic`, `collection_key=""`, `max_sources=20` | search → read full text → synthesize by theme → cite every claim with a real key |
| `find_related_work` | `item_key` | read item → extract concepts → search library → rank by relatedness |
| `check_citations` | `manuscript_text` | extract claimed citations → search the library for candidate matches → report candidates with match basis and confidence |
| `summarize_reading` | `item_key`, `audience="expert"` | fetch metadata + full text + own notes → structured summary |

`check_citations` is the anti-hallucination workflow and is a differentiator worth shipping in v1.

**Matching is recall-oriented; the caller filters.** Tools return candidates with the evidence for
each match rather than adjudicating identity server-side:

```python
class CitationMatch(BaseModel):
    matched_on: Literal["doi", "title", "title+year", "none"]
    confidence: float  # 0.0–1.0
    candidates: list[ItemSummary]  # may be empty, or several
```

DOI match first, then title, then title+year. A pre-print and its published version are *both*
returned when both are present — the server does not pick. This applies equally to
`find_item_by_identifier`, which returns candidates rather than a single item when identifier
matching is inconclusive.

Rationale: false negatives and near-duplicates are normal in search, and the client has context
the server does not (which venue the manuscript targets, whether a pre-print citation is
acceptable). Suppressing plausible matches server-side destroys information the caller cannot
recover; returning them with a stated match basis lets the caller drop what it does not want. The
tool descriptions must say this explicitly, so an agent treats results as candidates and not as
verdicts.

---

## 6. Data model: token efficiency

Raw Zotero item JSON carries `library`, `links`, `meta`, and every empty field of the item type —
often 2–4 KB per item, mostly noise for a model. **Projection is a core requirement, not an
optimization.**

`ItemSummary` (search results, ~150 tokens):

```python
class ItemSummary(BaseModel):
    key: str  # 8-char Zotero key; use for all follow-up calls
    item_type: str  # e.g. "journalArticle"
    title: str
    creators: str  # "Vaswani et al." — collapsed, not a nested array
    year: int | None
    publication: str | None
    doi: str | None
    tags: list[str]
    collections: list[str]  # collection names, not keys
    has_fulltext: bool  # can get_item_fulltext succeed?
    num_attachments: int
    num_notes: int
    date_added: date
```

`ItemDetail` adds abstract, full creator list with roles, pages/volume/issue, URL, extra,
`date_modified`, and `version`.

Rules:

- Omit null/empty fields from serialized output entirely.
- `creators` collapses to a display string in summaries; the full array appears only in
  `ItemDetail`.
- `has_fulltext` prevents a whole class of wasted round-trip.
- Never return `links` or `library` blocks to the model.

**Acceptance criterion:** a 25-item `search_items` response is ≤ 4,000 tokens.

---

## 7. Technical design

### 7.1 Stack

- Python **3.12+** (this said 3.13+ while the code ran on the repository's shared 3.12
  environment; nothing here needs 3.13, and requiring it would have made the package
  uninstallable in the environment it is actually tested in), `fastmcp`, `pyzotero`,
  `pydantic`, `pydantic-settings`
- Three deployment modes — STDIO, Streamable HTTP, in-memory (§7.2)
- ~~Logfire instrumentation~~ — **not implemented.** Stated here from the start and never
  built; the server logs via `logging` to stderr instead (§7.7's "never stdout" constraint
  is what actually matters for STDIO). Recorded rather than quietly dropped.

Client construction happens in `lifespan` (guide §2.2), never at import.

### 7.1a Distribution

This is a **standalone distribution**, not a directory on the parent repository's `sys.path`:
its own `pyproject.toml`, lock file, and `.venv`, with bounded dependency ranges (an upper
bound on every shipped dependency — `fastmcp<4` most importantly, since FastMCP 4 changes how
task-augmented execution is negotiated and this server has only run against 3.x).

Two things this buys that the previous arrangement could not:

- **A console script.** `zotero-mcp` is what an MCP client's `command:` points at — no
  interpreter path, no `python -m`, no working directory to get right.
- **In-memory embedding in another installed tool.** The package declares a
  `deep_research.mcp_servers` entry point resolving to `build_server`, a zero-argument factory
  returning a configured `FastMCP`. That is how a host application can discover this server
  as a bundled, in-process tool source without importing anything by
  name from a config file. A server that only existed as a `pythonpath` entry in someone else's
  `pyproject.toml` could not be installed into a *different* tool's environment at all, which
  made in-memory use impossible in practice however well §7.2 supported it in principle.

One consequence worth stating because it is invisible and load-bearing: `ZoteroSettings`
declares `env_file=".env"`, a path relative to the **process** working directory. An embedded
server therefore reads the *host's* `.env`, not one beside this package — so a single installed
copy serves different libraries depending on which project folder the host was launched from.
That is the intended behaviour for the harness (credentials sit beside its model API keys), and
there is a test asserting it so it does not change by accident.

### 7.2 Deployment modes and startup

The server must support all three FastMCP transports, selectable at startup. This is a
first-class requirement, not a deployment detail: the in-memory mode in particular changes how the
module must be structured.

| Mode | Transport | Use |
|---|---|---|
| **STDIO** (default) | stdio | Local agent launches the server as a subprocess. Claude Desktop, Claude Code, local PydanticAI. |
| **Streamable HTTP** | `http` | Long-lived server shared by several agents, or an agent on another host. Requires auth (§8). |
| **In-memory** | `FastMCPTransport` | Agent imports the server and runs it inside its own process — no subprocess, no socket, no serialization hop. |

SSE is not supported. It is deprecated upstream (guide §6.2) and there is no legacy Zotero client
to accommodate.

#### Entry point

```
uv run -m zotero_mcp                                  # stdio (default)
uv run -m zotero_mcp --transport stdio
uv run -m zotero_mcp --transport http --port 8000     # binds 127.0.0.1 by default
uv run -m zotero_mcp --transport http --host 0.0.0.0 --port 8000 --path /mcp
```

Resolution order for every startup option: **CLI flag > environment variable > default.** CLI
flags exist so a single installed package can serve several agent configurations without editing
the environment; env vars exist because STDIO clients configure servers that way.

#### In-memory mode

For in-memory use the module must expose both a ready instance and a factory, so a host process
can configure the server programmatically instead of through the environment:

```python
# zotero_mcp/server.py
mcp = create_server()  # module-level, env-configured


def create_server(settings: ZoteroSettings | None = None) -> FastMCP:
    """Build a configured server. Settings default to environment-derived."""
```

Embedding in an agent process:

```python
from fastmcp import Client
from zotero_mcp.server import create_server

server = create_server(
    ZoteroSettings(
        api_key=key,
        library_id="123456",
        library_type="user",
        allow_writes=False,
    )
)

async with Client(server) as client:  # FastMCPTransport, in-process
    results = await client.call_tool("search_items", {"query": "attention"})
```

Requirements this imposes:

- **No import-time side effects.** No pyzotero client, no network, no config validation at import
  — `create_server()` and `lifespan` own all of it (guide §2.2). A module that dials Zotero on
  import cannot be embedded.
- **Settings injectable as an object**, not read only from `os.environ`. The host process may hold
  the credential in memory and never set an env var.
- **`run_async()` when a host loop already exists.** In-memory embedding runs inside the agent's
  event loop; `mcp.run()` would try to start a second one.
- **Lifespan runs under the client's context manager**, so the pyzotero client and caches are
  created on `async with Client(server)` and torn down on exit. Cache lifetime is the client
  session, not the process.
- **Same tool surface in all three modes.** In-memory must not become a privileged path — the
  writes flag and read-only posture apply identically. The one legitimate difference: no `auth=`,
  since there is no transport boundary to authenticate across.

In-memory is also the testing transport (§9), so this structure is required regardless.

### 7.3 Pagination and bounded output

pyzotero defaults `limit=100`; the API default is 25 and its max is 100.

- Every list tool takes `limit` with `Field(ge=1, le=100)` and a default of 25.
- **`zot.everything()` is banned in tool paths.** It will happily pull 5,000 items and blow the
  context window. Use it only in explicit maintenance scripts.
- Report `total_matched` from the `Total-Results` header and set `truncated` whenever
  `returned < total_matched`, with a hint to narrow the query or raise `limit`.

### 7.4 Rate limits, retries, caching

The Zotero API signals throttling via a `Backoff` header and `429` with `Retry-After`, and
recommends ≤ 4 concurrent requests.

- Honor `Backoff` and `Retry-After`; exponential retry with jitter, capped at 3 attempts.
- Cap in-flight upstream requests at 4 with a semaphore.
- Cache collections, tags, and the item-type schema in-process, keyed on
  `last_modified_version()`; invalidate when it advances. These are read on nearly every workflow
  and change rarely.
- Surface a clear `ToolError` on sustained 429 — "Zotero API rate limit; retry in N seconds" —
  so the agent waits rather than hammering.

### 7.5 Write safety

Non-negotiable for any write tool:

- **Version-checked writes only.** Send the item's `version` (or `If-Unmodified-Since-Version`) so
  a concurrent desktop edit yields `412` instead of a silent overwrite.
- **PATCH, never PUT.** PUT deletes every omitted field — catastrophic when a model supplies a
  partial object. `update_item_fields` merges.
- **`Zotero-Write-Token`** on retryable creates, for idempotency (server caches 12 h).
- **Batch ≤ 50 objects** per request, the API ceiling.
- **Report partial failure faithfully.** Zotero returns `successful` / `unchanged` / `failed` by
  index; map all three into `BatchResult` rather than declaring success on a 200.

### 7.6 Local vs web

pyzotero's `local=True` reads from the Zotero 7 desktop local API — no key, no rate limit, no
network, read-only. Support it via `ZOTERO_LOCAL=true` for privacy-sensitive users, with write
tools force-disabled and `get_library_info` reporting the mode.

### 7.7 Errors

Per guide §3.5 — `ToolError` for actionable failures, everything else masked:

| Condition | Message |
|---|---|
| Bad/missing API key | "Zotero API key rejected. Verify `ZOTERO_API_KEY` and its library permissions." |
| Unknown item key | "No item with key `ABC12345`. Use `search_items` to find valid keys." |
| No full text | "No indexed full text for `ABC12345` (attachments: 1 link-only). Try `get_item` for metadata." |
| Invalid CSL style | "Unknown style `apa7`. Try `apa`, `chicago-note-bibliography`, `mla`." |
| Write attempted, writes off | "Writes are disabled. Set `ZOTERO_ALLOW_WRITES=true` to enable." |
| `412` version conflict | "Item changed in Zotero since it was read. Call `get_item` again and retry." |

Never leak the API key, user ID, or filesystem paths.

---

## 8. Configuration and security

**Library and behaviour**

| Variable | Required | Purpose |
|---|---|---|
| `ZOTERO_API_KEY` | yes (unless local) | Web API key |
| `ZOTERO_LIBRARY_ID` | yes | Numeric user or group ID |
| `ZOTERO_LIBRARY_TYPE` | no (`user`) | `user` \| `group` |
| `ZOTERO_ALLOW_WRITES` | no (`false`) | Enables §5.5 tools |
| `ZOTERO_LOCAL` | no (`false`) | Desktop local API, read-only |
| `ZOTERO_DEFAULT_STYLE` | no (`chicago-note-bibliography`) | Default CSL style |
| `ZOTERO_FULLTEXT_MAX_CHARS` | no (`100000`) | Default full-text truncation ceiling; overridable per call via `max_chars` |

**Transport** (each with an equivalent CLI flag, which takes precedence — §7.2)

| Variable | CLI flag | Default | Purpose |
|---|---|---|---|
| `ZOTERO_MCP_TRANSPORT` | `--transport` | `stdio` | `stdio` \| `http` |
| `ZOTERO_MCP_HOST` | `--host` | `127.0.0.1` | HTTP bind address |
| `ZOTERO_MCP_PORT` | `--port` | `8000` | HTTP port |
| `ZOTERO_MCP_PATH` | `--path` | `/mcp` | HTTP mount path |
| `ZOTERO_MCP_AUTH_TOKEN` | — | none | Bearer token verified on HTTP |
| `FASTMCP_LOG_LEVEL` | `--log-level` | `INFO` | Verbosity (guide §8) |

Validate at startup and refuse to boot on a missing key or an ID that `key_info()` rejects (guide
§2.3). API key only in v1 — OAuth adds a browser redirect flow that buys nothing for a
single-user local server. Recommend a **read-only** key for default installs.

**Transport-specific security.** This server is a read channel into a personal library, so the
posture differs per mode:

- **STDIO** — no transport auth needed; the parent process already has the user's privileges.
- **In-memory** — no transport boundary, so no `auth=`. The host process is the trust boundary.
- **HTTP** — `auth=` is **mandatory**. Starting with `--transport http` and no
  `ZOTERO_MCP_AUTH_TOKEN` must **refuse to boot**, not warn. Binding to anything other than
  `127.0.0.1` requires the token *and* an explicit `--host`; there is no path to an
  unauthenticated public listener.

---

## 9. Testing

Per guide §9, in-memory `Client(mcp)` against a mocked pyzotero layer.

1. **Schema surface** — expected tool names; write tools absent when the flag is off.
2. **Projection** — a recorded raw Zotero payload maps to `ItemSummary` with no `links`/`meta`
   leakage; token budget (§6) asserted.
3. **Pagination** — `truncated` set correctly; `limit` clamped to 100.
4. **Rate limiting** — `Backoff`/`429` responses trigger retry, then a clean `ToolError`.
5. **Write safety** — PATCH not PUT; version sent; >50-key batch rejected; partial-failure
   response mapped faithfully.
6. **Resource templates** — malformed keys raise `ResourceError`, not upstream 404 noise.
7. **Matching recall** — a pre-print/published pair returns *both* candidates with correct
   `matched_on` values; a fabricated citation returns `matched_on="none"` and an empty candidate
   list. Assert the server never silently picks one of several plausible matches.
8. **Full-text ceiling** — per-call `max_chars` overrides the env default; `truncated` and
   `total_chars` are accurate at the boundary.
9. **Startup matrix** — each transport starts and serves an identical tool surface; `--transport
   http` without `ZOTERO_MCP_AUTH_TOKEN` exits non-zero; CLI flags override env vars; no module
   imports trigger network I/O (assert with a blocked-socket fixture).
10. **Integration suite** against a small real fixture library, run manually before release.

Golden fixtures come from recorded real API responses; no live network in unit tests.

---

## 10. Milestones

| Phase | Contents | Exit criterion |
|---|---|---|
| **M1 — Read core** | `get_library_info`, `search_items`, `get_item`, `list_collections`, `list_collection_items`, `list_tags`; projection layer; `create_server()` factory; STDIO + in-memory | Agent answers a topic question citing real keys, over both transports |
| **M2 — Full text** | `get_item_fulltext`, `get_item_notes`, `get_item_children`, fulltext search mode, truncation | Agent summarizes a PDF from a key alone |
| **M3 — Citations** | `format_citation`, `format_bibliography`, `export_items`, resources | Zero-edit bibliography in a chosen style |
| **M4 — Prompts + transports** | 4 prompts, caching, retry/backoff, Logfire, HTTP transport with auth, CLI flags, README | `check_citations` reports a fabricated reference as unmatched; HTTP refuses to boot unauthenticated |
| **M5 — Writes (opt-in)** | §5.5 tools with full §7.5 safety | Version-conflict and partial-failure tests green |
| **M6 — Standalone distribution** *(done)* | Own `pyproject.toml`/lock/`.venv` with bounded dependency ranges, `zotero-mcp` console script, `deep_research.mcp_servers` entry point, `.gitignore` covering `.env` (§7.1a) | `pipx install <this dir>` yields a working `zotero-mcp`; the 80-test suite runs from this directory against the installed package; a host application runs it in-memory with no subprocess |

M1–M4 constitute the shippable v1; M5 is separately gated. **M6 was not in the original plan** —
it was pulled in because in-memory embedding in another installed tool turned out to be impossible
without it, however completely §7.2 specified the in-memory *code* path.

Not done, and required before any publication: **the `zotero-mcp` distribution name is very likely
already taken.** The References section above links an unrelated `zotero-mcp` project, so this is a
concrete collision rather than a hypothetical one. The name is currently only used locally, where
nothing conflicts. A rename is cheap and need not disturb consumers: the distribution name, the
import package (`zotero_mcp`), the console script, and the entry-point name are chosen
independently, and the harness's `config.yaml` refers only to the **entry-point name**.

---

## 11. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Full-text payloads exhaust the context window | High | `max_chars` ceiling, `truncated` flag, windowing in v2 |
| Verbose Zotero JSON makes tools costly | High | Projection layer (§6) with an asserted token budget |
| Model calls destructive operations | High | No delete tools at all in v1; writes flag-gated |
| Agent overwrites fields via PUT semantics | High | PATCH-only merge (§7.5) |
| Rate limiting during multi-item workflows | Medium | Backoff, concurrency cap, caching |
| Library too small to be useful | Medium | `get_library_info` orientation; prompts state when to fall back to web search |
| Zotero's search is weak for conceptual queries | Medium | Document the limit; semantic layer in v2 |
| Full-text unavailable (link-only attachments) | Medium | `has_fulltext` in summaries; explicit error text |
| HTTP mode exposes a personal library on the network | High | Token required to boot; `127.0.0.1` default; no unauthenticated listener possible (§8) |
| In-memory mode inherits the host process's memory and environment | Medium | Documented as a trust boundary, not a sandbox; identical writes gating in all modes (§7.2) |

---

## 12. Future (post-v1)

- **Semantic search** — local embeddings over indexed full text, exposed as a `mode="semantic"`
  option on `search_items`. Highest-value follow-on.
- **Full-text windowing** — `section`/offset parameters and per-section retrieval, so an agent can
  read a paper's methods without its introduction.
- **PDF annotation access** — highlights and comments are, like notes, direct records of the
  researcher's judgment.
- **Multi-library** — query personal and group libraries in one call.
- **Group permission reporting** — surface a group's per-user permission level in
  `get_library_info` so an agent can predict a write failure instead of discovering it (D1).
- **Attachment upload**, `add_by_identifier` (DOI → item via translation server), saved-search
  execution, duplicate detection.

---

## 13. Resolved decisions

Recorded so the reasoning survives into implementation.

| # | Question | Decision |
|---|---|---|
| D1 | Should `get_library_info` surface group per-user write permissions? | **No, not in v1.** An agent discovers a write failure via the `412`/`403` error path, which is adequate. Deferred to a future version (§12). |
| D2 | What is the full-text ceiling? | **Configurable, default 100,000 characters.** Per-call `max_chars` > `ZOTERO_FULLTEXT_MAX_CHARS` > 100,000 (§5.2). No auto-scaling to the client's context window. |
| D3 | How should citation matching handle pre-print vs published duplicates? | **Recall-oriented — return candidates, let the client filter.** DOI → title → title+year, with `matched_on` and `confidence` on every match; near-duplicates are all returned (§5.7). |

D3 generalizes into a server-wide principle worth stating once: **this server retrieves and
reports; it does not adjudicate.** Where a judgment call depends on context the server lacks, it
returns the evidence and lets the caller decide.

---

## Assumptions

Stated explicitly, since they shaped the scope above and are cheap to revise:

- Single user, single library. STDIO and in-memory are the primary deployment targets; HTTP is a
  secondary path for shared or remote use.
- Read-only is the default posture; the researcher's library is treated as precious and
  effectively irreplaceable.
- Zotero's own search and full-text index are sufficient for v1; no retrieval infrastructure is
  built.
- Consumers are PydanticAI or Claude-based agents.
