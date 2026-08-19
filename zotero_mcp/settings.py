"""Configuration for the Zotero MCP server.

Settings are read from the environment but are always injectable as an object, so a
host process embedding the server in-memory never has to touch ``os.environ``
(PRD 7.2). Nothing here performs I/O: validation is pure, and the pyzotero client is
built later, inside the server lifespan.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Transport = Literal["stdio", "http"]
LibraryType = Literal["user", "group"]

#: Default full-text ceiling in characters (~25-30k tokens). PRD decision D2.
DEFAULT_FULLTEXT_MAX_CHARS = 100_000


class ConfigurationError(RuntimeError):
    """Raised at startup when configuration is unusable.

    The server must refuse to boot rather than fail on every call (PRD 8).
    """


class ZoteroSettings(BaseSettings):
    """Runtime configuration for one Zotero library and one transport."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- library -----------------------------------------------------------
    api_key: str | None = Field(default=None, validation_alias="ZOTERO_API_KEY")
    library_id: str | None = Field(default=None, validation_alias="ZOTERO_LIBRARY_ID")
    library_type: LibraryType = Field(
        default="user", validation_alias="ZOTERO_LIBRARY_TYPE"
    )
    local: bool = Field(default=False, validation_alias="ZOTERO_LOCAL")
    locale: str = Field(default="en-US", validation_alias="ZOTERO_LOCALE")

    # --- behaviour ---------------------------------------------------------
    allow_writes: bool = Field(default=False, validation_alias="ZOTERO_ALLOW_WRITES")
    default_style: str = Field(
        default="chicago-note-bibliography",
        validation_alias="ZOTERO_DEFAULT_STYLE",
    )
    fulltext_max_chars: int = Field(
        default=DEFAULT_FULLTEXT_MAX_CHARS,
        ge=1_000,
        validation_alias="ZOTERO_FULLTEXT_MAX_CHARS",
    )
    max_concurrency: int = Field(
        default=4, ge=1, le=8, validation_alias="ZOTERO_MAX_CONCURRENCY"
    )

    # --- transport ---------------------------------------------------------
    transport: Transport = Field(
        default="stdio", validation_alias="ZOTERO_MCP_TRANSPORT"
    )
    host: str = Field(default="127.0.0.1", validation_alias="ZOTERO_MCP_HOST")
    port: int = Field(default=8000, ge=1, le=65535, validation_alias="ZOTERO_MCP_PORT")
    path: str = Field(default="/mcp", validation_alias="ZOTERO_MCP_PATH")
    auth_token: str | None = Field(
        default=None, validation_alias="ZOTERO_MCP_AUTH_TOKEN"
    )

    @model_validator(mode="after")
    def _normalize(self) -> ZoteroSettings:
        if self.local:
            # The desktop local API is read-only and unauthenticated by design.
            object.__setattr__(self, "allow_writes", False)
        return self

    # ------------------------------------------------------------------ checks
    def validate_for_startup(self) -> None:
        """Fail loudly before serving. Raises :class:`ConfigurationError`."""
        problems: list[str] = []

        if not self.local:
            if not self.api_key:
                problems.append(
                    "ZOTERO_API_KEY is not set (required unless ZOTERO_LOCAL=true)"
                )
            if not self.library_id:
                problems.append("ZOTERO_LIBRARY_ID is not set")
            elif not str(self.library_id).isdigit():
                problems.append(
                    f"ZOTERO_LIBRARY_ID must be numeric, got {self.library_id!r} "
                    "(it is the numeric ID from your Zotero API keys page, "
                    "not your username)"
                )

        # HTTP must never listen without authentication (PRD 8).
        if self.transport == "http" and not self.auth_token:
            problems.append(
                "ZOTERO_MCP_AUTH_TOKEN is required when --transport http. "
                "This server is a read channel into a personal library; there is "
                "no unauthenticated HTTP mode."
            )

        if problems:
            raise ConfigurationError(
                "Invalid configuration:\n  - " + "\n  - ".join(problems)
            )

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}
