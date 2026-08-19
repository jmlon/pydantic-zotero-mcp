"""Command-line entry point.

Startup option precedence is CLI flag > environment variable > default (PRD 7.2), so
one installed package can serve several agent configurations without editing the
environment.

Logging goes to stderr unconditionally: under stdio, stdout is the JSON-RPC channel
and a single stray write to it corrupts the stream (guide 8).
"""

from __future__ import annotations

import argparse
import logging
import sys

from .settings import ConfigurationError, ZoteroSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zotero-mcp",
        description="MCP server exposing a Zotero library to AI agents.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=None,
        help="Transport to serve on (default: stdio, or ZOTERO_MCP_TRANSPORT)",
    )
    parser.add_argument("--host", default=None, help="HTTP bind address")
    parser.add_argument("--port", type=int, default=None, help="HTTP port")
    parser.add_argument("--path", default=None, help="HTTP mount path")
    parser.add_argument(
        "--library-id", default=None, help="Zotero numeric user or group ID"
    )
    parser.add_argument(
        "--library-type", choices=("user", "group"), default=None, help="Library type"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        default=None,
        help="Read the Zotero desktop local API (read-only, no API key)",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        default=None,
        help="Enable write tools (off by default)",
    )
    parser.add_argument(
        "--fulltext-max-chars",
        type=int,
        default=None,
        help="Default full-text truncation ceiling",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    return parser


def settings_from_args(argv: list[str] | None = None) -> ZoteroSettings:
    """Merge CLI flags over environment-derived settings."""
    args = build_parser().parse_args(argv)
    settings = ZoteroSettings()

    overrides = {
        "transport": args.transport,
        "host": args.host,
        "port": args.port,
        "path": args.path,
        "library_id": args.library_id,
        "library_type": args.library_type,
        "local": args.local,
        "allow_writes": args.allow_writes,
        "fulltext_max_chars": args.fulltext_max_chars,
    }
    applied = {k: v for k, v in overrides.items() if v is not None}
    if applied:
        # Re-validate through the model so CLI values get the same checks as env ones.
        settings = settings.model_copy(update=applied)
        settings = ZoteroSettings.model_validate(settings.model_dump())

    if args.log_level:
        logging.getLogger().setLevel(args.log_level.upper())
    return settings


def configure_logging(level: str = "INFO") -> None:
    """Send all logging to stderr. Never stdout."""
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    try:
        settings = settings_from_args(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - any bad option becomes exit code 2
        print(f"zotero-mcp: invalid options: {exc}", file=sys.stderr)
        return 2

    from .server import create_server

    try:
        server = create_server(settings)
    except ConfigurationError as exc:
        print(f"zotero-mcp: {exc}", file=sys.stderr)
        return 2

    if settings.transport == "http":
        if not settings.is_loopback:
            logging.getLogger(__name__).warning(
                "binding %s: this exposes a personal library beyond localhost",
                settings.host,
            )
        server.run(
            transport="http",
            host=settings.host,
            port=settings.port,
            path=settings.path,
        )
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
