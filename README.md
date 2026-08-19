# zotero-mcp

An MCP server that gives AI agents read access to a Zotero library — search, item
metadata, collections, tags, the researcher's own notes, and the indexed full text of
attached PDFs.

See [PRD.md](PRD.md) for requirements and [../fastmcp-guide.md](../fastmcp-guide.md) for
the FastMCP conventions this follows.

**Status: M1 (read core) + M2 (full text) implemented.** Citation formatting and
export (M3), prompts (M4), and write tools (M5) are not built yet — see
[Not yet implemented](#not-yet-implemented).

## Install

This is a **standalone package** — its own `pyproject.toml` and lock file, independent of
the surrounding `PydanticAI` project's environment. It can be installed on its own, and
consumers (an MCP client, or the `deep-research` harness) never need this source tree's
parent.

### As a tool (`pipx`)

Installs the `zotero-mcp` command into its own isolated environment:

```bash
pipx install /path/to/PydanticAI/MCP/ZoteroMCP
zotero-mcp --help
```

### Into another project's environment

```bash
uv add /path/to/PydanticAI/MCP/ZoteroMCP     # or: uv pip install <path>
pipx inject deep-research-harness /path/to/PydanticAI/MCP/ZoteroMCP
```

The second form is what makes this server available to `deep-research` as an in-memory
tool source — see [Embedding in `deep-research`](#embedding-in-deep-research).

### For development on this server

```bash
cd /path/to/PydanticAI/MCP/ZoteroMCP
uv sync           # creates ./.venv from this project's own lock file
uv run pytest
uv run ruff check
```

Nothing here reads the parent project's environment. Its test suite is likewise no longer
collected by a root-level `pytest` run — run it from this directory.

## Configure

Get a **read-only** API key and your numeric user ID from
<https://www.zotero.org/settings/keys>. The library ID is the number, not your
username.

```bash
export ZOTERO_API_KEY=...
export ZOTERO_LIBRARY_ID=123456      # numeric
export ZOTERO_LIBRARY_TYPE=user      # or group
```

| Variable | Default | Purpose |
|---|---|---|
| `ZOTERO_API_KEY` | — | Web API key (required unless `ZOTERO_LOCAL=true`) |
| `ZOTERO_LIBRARY_ID` | — | Numeric user or group ID |
| `ZOTERO_LIBRARY_TYPE` | `user` | `user` or `group` |
| `ZOTERO_LOCAL` | `false` | Read the Zotero 7 desktop API instead: no key, no rate limit, read-only |
| `ZOTERO_ALLOW_WRITES` | `false` | Reserved for M5; no write tools exist yet |
| `ZOTERO_FULLTEXT_MAX_CHARS` | `100000` | Default full-text ceiling; per-call `max_chars` overrides it |
| `ZOTERO_DEFAULT_STYLE` | `chicago-note-bibliography` | Reserved for M3 |
| `ZOTERO_MAX_CONCURRENCY` | `4` | Upstream request cap (Zotero asks for ≤ 4) |
| `ZOTERO_MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `ZOTERO_MCP_HOST` | `127.0.0.1` | HTTP bind address |
| `ZOTERO_MCP_PORT` | `8000` | HTTP port |
| `ZOTERO_MCP_PATH` | `/mcp` | HTTP mount path |
| `ZOTERO_MCP_AUTH_TOKEN` | — | Bearer token; **required** for HTTP |

CLI flags override environment variables.

## Run

Once installed, `zotero-mcp` is the entry point — no interpreter path, no `python -m`, no
working directory to get right, which is what an MCP client's `command:` wants:

```bash
# stdio (default) — an agent launches this as a subprocess
zotero-mcp

# streamable HTTP — requires ZOTERO_MCP_AUTH_TOKEN
ZOTERO_MCP_AUTH_TOKEN=secret zotero-mcp --transport http --port 8000

# read the Zotero desktop app instead of the web API
zotero-mcp --local
```

From a checkout, without installing, `python -m zotero_mcp` still works:

```bash
uv run python -m zotero_mcp
```

Starting with `--transport http` and no token exits 2 rather than serving
unauthenticated: this is a read channel into a personal library.

### In-memory (embedded in an agent process)

No subprocess, no socket. Settings are injected, so the host never needs environment
variables:

```python
from fastmcp import Client
from zotero_mcp import ZoteroSettings, create_server

server = create_server(
    ZoteroSettings(
        api_key=key,
        library_id="123456",
        library_type="user",
    )
)

async with Client(server) as client:  # lifespan opens here
    result = await client.call_tool("search_items", {"query": "attention"})
    print(result.structured_content["items"])  # dict; result.data is a model
```

Importing `zotero_mcp` has no side effects — no config read, no client built, no
network — which is what makes embedding possible. There is a test that enforces it.

### Embedding in `deep-research`

The [deep-research harness](../../Harness/deepResearch/) can use this server as an
**in-memory tool source**: imported and run inside the harness process, no subprocess and
no socket. It discovers bundled servers through a `deep_research.mcp_servers` entry point,
which this package declares:

```toml
[project.entry-points."deep_research.mcp_servers"]
zotero = "zotero_mcp:build_server"
```

`build_server()` takes no arguments and derives settings from the environment, which is the
factory contract the harness expects. To use it, install this package into the harness's
environment and name it in the project's `config.yaml`:

```bash
pipx inject deep-research-harness /path/to/PydanticAI/MCP/ZoteroMCP
```

```yaml
mcp_servers:
  - name: "zotero"            # matches the entry-point name above
    transport: "in_memory"
    health_check: "get_library_info"
    tool_args:
      get_item_fulltext: {max_chars: 20000}   # the default is 100,000 — see below
```

Credentials come from the harness project's `.env`, exactly as its model API keys do. Note
`max_chars`: this server's default full-text ceiling is 100,000 characters (~25–30k tokens
for a *single* call), which is generous for interactive use and far too large for a research
run making many calls against a token budget.

## Tools

| Tool | Purpose |
|---|---|
| `get_library_info` | Size, mode, permissions. Cheap orientation call — use it first |
| `search_items` | Primary entry point. `mode="metadata"` or `"fulltext"` (searches PDF text) |
| `list_recent_items` | Recently added items, newest first |
| `find_item_by_identifier` | "Do I already have this?" by DOI, ISBN, arXiv ID, or key |
| `get_item` | Full metadata; `include_children=True` also lists attachments and notes |
| `get_item_children` | Attachments and notes, with `may_have_fulltext` per attachment |
| `get_item_notes` | The researcher's own notes, HTML stripped |
| `get_item_fulltext` | Indexed attachment text; resolves parent → attachment |
| `list_collections` | Nested collection tree |
| `list_collection_items` | Items in one collection |
| `list_tags` | Tag vocabulary, optionally prefix-filtered |

Resources: `zotero://library/info`, `zotero://collections`,
`zotero://items/{key}`, `zotero://items/{key}/fulltext`,
`zotero://collections/{key}/items`, `zotero://schema/item-types`,
`zotero://schema/item-types/{type}/fields`.

## Design notes

**Projection is the point.** Raw Zotero JSON is ~1 KB per item of `links`, `library`,
`meta`, and empty type fields. `zotero_mcp/projection.py` reduces a 25-item page from
~6,100 to ~2,400 estimated tokens (39% of raw), under the PRD's 4,000 budget. Null
fields are dropped at serialization by `CompactModel`.

**pyzotero is synchronous and stateful.** `Zotero.request` and `Zotero.links` are
overwritten by each call, and `Total-Results` is read back off the instance
afterwards — so one shared client used concurrently would report another call's
totals. `gateway.py` keeps a pool of up to `ZOTERO_MAX_CONCURRENCY` clients, checks
one out per operation, and reads response metadata inside the same worker thread that
holds it. Every call goes through `anyio.to_thread.run_sync` so the event loop never
blocks.

**Backoff is pyzotero's job.** pyzotero ≥ 1.13 already honours `Backoff` /
`Retry-After` and retries 429 internally, so the gateway does not reimplement it. It
adds a bounded 3-attempt retry for transient transport and 5xx failures only.

**Nothing is silently truncated.** Searches report `total_matched`, `truncated`, and
`next_start`; full text reports `total_chars` and `truncated`.

**Results are candidates, not verdicts** (PRD D3). `find_item_by_identifier` returns
`matched_on` (`key` / `doi` / `title` / `identifier` / `none`) plus a confidence and
*all* plausible candidates — a pre-print and its published version both survive. The
caller filters.

## Deviations from the PRD

Worth knowing about, since each was a judgment call made during implementation:

1. **No module-level `mcp` object.** PRD 7.2 asked for both a module-level
   `mcp = create_server()` and no import-time side effects. Those conflict: building
   the server validates config, so a module-level instance raises `ImportError` on any
   machine without Zotero env vars, and breaks the in-memory path it was meant to
   support. Only `create_server()` / `build_default_server()` exist.
2. **Write tools will be registered conditionally, not `enabled=False`.** PRD 5.5
   specified `@mcp.tool(enabled=False)`, but FastMCP 3.x has no `enabled` kwarg, and a
   disabled-but-listed tool still costs context. When M5 lands, write tools will simply
   not be registered unless `ZOTERO_ALLOW_WRITES=true`.

   This server targets **FastMCP 3.x**, as does `../fastmcp-guide.md` (see its §12 for the
   2.x migration table). Two 3.x specifics shape the code here: `enabled` is gone from
   the decorators, and `result.data` is a generated pydantic model while
   `result.structured_content` is the plain dict — the tests assert on the latter, which
   also verifies null-omission on the wire.
3. **`has_fulltext` is three-valued.** PRD 6 typed it `bool`, but determining it for a
   parent item requires a separate children request per item, which would make a
   25-item search 26 requests. It is `False` when an item has no children at all,
   `True`/`False` for attachments and after `get_item(include_children=True)`, and
   `null` (omitted) when undetermined. `ItemSummary.num_children` gives the cheap
   signal.
4. **`find_item_by_identifier` returns `CitationMatch`, not `ItemSummary | None`.**
   Follows from D3 — the old signature made exactly the identity call that decision
   moved to the client.
5. **`matched_on` gained `key` and `identifier`** beyond the PRD's four values, to
   distinguish an exact key hit from a weak search hit.
6. **`list_recent_items(since_days=...)` filters locally.** Zotero has no server-side
   date filter, so a narrow window can return fewer items than `limit`; the response
   `hint` says when that happened.

## Tests

```bash
cd /home/jmlon/GIT/AWS/AI/PydanticAI
uv run python -m pytest MCP/ZoteroMCP/tests -q      # 65 passed
```

Scope the path to `MCP/ZoteroMCP/tests`: a bare `uv run pytest` at the repository root
aborts during collection on unrelated pre-existing suites (`demos/test_postgres.py`
opens a Postgres connection at import, and two `test_calc.py` files under
`Harness/tutorial/` collide on module basename).

The suite uses FastMCP's in-memory transport against a `FakeZotero` that reproduces
pyzotero's read-metadata-off-the-instance behaviour. No network, no subprocess, no
real credentials. Coverage: schema surface, projection and token budget, pagination
and truncation reporting, full-text ceiling and parent resolution, match recall
(pre-print/published pairs both returned), error message quality, resource template
validation including traversal attempts, config validation, CLI precedence, gateway
retry/caching, and an import-purity check that fails if importing the package touches
the network.

## Not yet implemented

- **M3** — `format_citation`, `format_bibliography`, `export_items`
- **M4** — the four prompts (`literature_review`, `find_related_work`,
  `check_citations`, `summarize_reading`), Logfire instrumentation
- **M5** — write tools (`create_item`, `update_item_fields`, `add_item_tags`,
  `add_items_to_collection`, `create_note`) with version-checked PATCH semantics.
  Deletion is out of scope permanently.
