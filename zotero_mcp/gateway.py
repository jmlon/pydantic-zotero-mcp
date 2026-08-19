"""Async gateway over pyzotero.

Two facts about pyzotero drive this design:

1. **It is synchronous** (httpx.Client), so every call must be pushed off the event
   loop or the whole server stalls (guide 3.6).
2. **It is stateful.** ``Zotero.request`` and ``Zotero.links`` are overwritten by each
   call, and response metadata like ``Total-Results`` is read back off the instance
   afterwards. One shared instance used by concurrent tasks would interleave and
   report another call's totals.

So instead of one client, this keeps a small pool — at most
``settings.max_concurrency`` instances (Zotero asks for <= 4 concurrent requests) —
and checks one out per operation. Response metadata is read inside the same worker
thread that made the call, while that instance is exclusively held.

Backoff: pyzotero >= 1.13 already honours the server's ``Backoff``/``Retry-After``
headers and retries 429 internally, so this layer does not reimplement it. What it
adds is a bounded retry for transient transport/5xx failures, which pyzotero raises
straight through.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio

from .settings import ZoteroSettings

logger = logging.getLogger(__name__)

#: Bounded retry for transient upstream failures.
MAX_ATTEMPTS = 3
RETRY_DELAYS = (0.5, 2.0)

#: How long a cached library-version probe stays fresh, in seconds. Bounds the cost
#: of cache validation without letting caches go stale for long.
VERSION_TTL = 30.0


class ZoteroUnavailable(RuntimeError):
    """Upstream failure that the caller cannot fix by changing arguments."""


class ZoteroNotFound(LookupError):
    """The requested object does not exist (HTTP 404)."""


class ZoteroForbidden(PermissionError):
    """Credentials rejected or insufficient (HTTP 403)."""


@dataclass(slots=True)
class Fetched[T]:
    """A payload plus the response metadata that came back with it."""

    value: T
    total_results: int | None = None
    library_version: int | None = None


@dataclass(slots=True)
class _CacheEntry:
    library_version: int | None
    value: Any


@dataclass(slots=True)
class _Pool:
    """Fixed-size pool of pyzotero clients, each used by one task at a time."""

    factory: Callable[[], Any]
    size: int
    _free: list[Any] = field(default_factory=list)
    _limiter: anyio.CapacityLimiter | None = None

    def _limit(self) -> anyio.CapacityLimiter:
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(self.size)
        return self._limiter

    @asynccontextmanager
    async def acquire(self):
        async with self._limit():
            client = self._free.pop() if self._free else self.factory()
            try:
                yield client
            finally:
                self._free.append(client)


def _build_pyzotero(settings: ZoteroSettings) -> Any:
    """Construct a real pyzotero client. Imported lazily to keep imports side-effect free."""
    from pyzotero.zotero import Zotero

    if settings.local:
        return Zotero(
            library_id=settings.library_id or "0",
            library_type=settings.library_type,
            local=True,
            locale=settings.locale,
        )
    return Zotero(
        library_id=settings.library_id,
        library_type=settings.library_type,
        api_key=settings.api_key,
        locale=settings.locale,
    )


def _status_of(exc: BaseException) -> int | None:
    """Dig an HTTP status code out of a pyzotero/httpx exception."""
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


#: pyzotero raises typed exceptions that usually carry no status code, so classify by
#: type name. Scanning the message for "404" is not an option — an 8-character Zotero
#: key such as "AB404XYZ" would misclassify a perfectly ordinary failure.
_NOT_FOUND_NAMES = frozenset({"ResourceNotFoundError", "CallDoesNotExistError"})
_FORBIDDEN_NAMES = frozenset(
    {"UserNotAuthorisedError", "MissingCredentialsError", "UnsupportedParamsError"}
)


def _classify(exc: BaseException) -> BaseException:
    """Map a pyzotero exception onto this module's error vocabulary."""
    name = type(exc).__name__
    if name in _NOT_FOUND_NAMES:
        return ZoteroNotFound(str(exc))
    if name in _FORBIDDEN_NAMES:
        return ZoteroForbidden(str(exc))

    status = _status_of(exc)
    if status == 404:
        return ZoteroNotFound(str(exc))
    if status in (401, 403):
        return ZoteroForbidden(str(exc))
    return ZoteroUnavailable(str(exc))


def _is_retryable(exc: BaseException) -> bool:
    status = _status_of(exc)
    if status is not None and status >= 500:
        return True
    # Transport-level failures carry no status.
    name = type(exc).__name__
    return name in {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "CouldNotReachURLError",
    }


class ZoteroGateway:
    """Async, pooled, cached access to one Zotero library."""

    def __init__(
        self,
        settings: ZoteroSettings,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._pool = _Pool(
            factory=client_factory or (lambda: _build_pyzotero(settings)),
            size=settings.max_concurrency,
        )
        self._cache: dict[str, _CacheEntry] = {}
        self._version: int | None = None
        self._version_checked_at: float = 0.0

    @property
    def settings(self) -> ZoteroSettings:
        return self._settings

    # ------------------------------------------------------------------ requests
    async def fetch[T](
        self, op: Callable[[Any], T], name: str = "request"
    ) -> Fetched[T]:
        """Run ``op`` against a checked-out client in a worker thread.

        ``op`` receives the pyzotero client. Response metadata is captured inside
        the thread, while this task still exclusively holds that client.
        """
        last_error: BaseException | None = None

        for attempt in range(MAX_ATTEMPTS):
            async with self._pool.acquire() as client:

                def _run() -> Fetched[T]:
                    value = op(client)
                    headers = getattr(getattr(client, "request", None), "headers", None)
                    headers = headers or {}
                    return Fetched(
                        value=value,
                        total_results=_as_int(headers.get("Total-Results")),
                        library_version=_as_int(
                            headers.get("Last-Modified-Version")
                            or headers.get("last-modified-version")
                        ),
                    )

                try:
                    result = await anyio.to_thread.run_sync(_run)
                except Exception as exc:  # re-raised below, classified
                    last_error = exc
                    if attempt + 1 < MAX_ATTEMPTS and _is_retryable(exc):
                        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                        logger.warning(
                            "zotero %s failed (%s); retrying in %.1fs",
                            name,
                            exc,
                            delay,
                        )
                        await anyio.sleep(delay)
                        continue
                    raise _classify(exc) from exc

            if result.library_version is not None:
                self._note_version(result.library_version)
            return result

        raise _classify(last_error or ZoteroUnavailable("unknown failure"))

    async def call[T](self, op: Callable[[Any], T], name: str = "request") -> T:
        """:meth:`fetch` without the response metadata."""
        return (await self.fetch(op, name)).value

    # -------------------------------------------------------------------- caching
    def _note_version(self, version: int) -> None:
        if self._version != version:
            if self._version is not None:
                logger.debug("library version %s -> %s", self._version, version)
            self._version = version
        self._version_checked_at = time.monotonic()

    async def library_version(self, *, force: bool = False) -> int | None:
        """Current library version, probed at most once per :data:`VERSION_TTL`."""
        fresh = time.monotonic() - self._version_checked_at < VERSION_TTL
        if not force and self._version is not None and fresh:
            return self._version
        try:
            version = await self.call(
                lambda z: z.last_modified_version(), name="last_modified_version"
            )
        except Exception:  # noqa: BLE001 - cache validation must not break reads
            logger.debug("library version probe failed; serving cache as-is")
            return self._version
        if isinstance(version, int):
            self._note_version(version)
        return self._version

    async def cached[T](self, key: str, loader: Callable[[], Any]) -> T:
        """Cache ``loader``'s result, invalidated when the library version moves.

        For collections, tags, and the item-type schema: read on nearly every
        workflow, changed rarely (PRD 7.4).
        """
        version = await self.library_version()
        entry = self._cache.get(key)
        if entry is not None and entry.library_version == version:
            return entry.value
        value = await loader()
        self._cache[key] = _CacheEntry(library_version=version, value=value)
        return value

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    # ------------------------------------------------------------------ lifecycle
    async def verify_credentials(self) -> dict[str, Any]:
        """Confirm the key works and report what it may do.

        Local mode has no key to verify, so this is skipped there.
        """
        if self._settings.local:
            return {"mode": "local"}
        return await self.call(lambda z: z.key_info(), name="key_info")

    async def aclose(self) -> None:
        """Close pooled httpx clients."""
        for client in list(self._pool._free):
            inner = getattr(client, "client", None)
            close = getattr(inner, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # best effort on shutdown
                    logger.debug("error closing pyzotero client", exc_info=True)
        self._pool._free.clear()
        self._cache.clear()


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
