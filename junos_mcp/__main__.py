"""Allow running as: python -m junos_mcp"""

from __future__ import annotations

import argparse
import os
import sys

from junos_mcp import __version__
from junos_mcp.server import _ensure_config, mcp


def _check_config() -> int:
    """Verify config.ini is loadable and list available routers."""
    err = _ensure_config("")
    if err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1
    from junos_ops import common

    sections = common.config.sections()
    print(f"OK: config loaded from {common.args.config}")
    print(f"Routers ({len(sections)}): {', '.join(sections) if sections else '(none)'}")
    return 0


def main() -> None:
    """Entry point for console_scripts."""
    parser = argparse.ArgumentParser(
        prog="junos-mcp",
        description=(
            "MCP server for junos-ops. Runs a JSON-RPC server exposing "
            "Juniper Networks device operations (CLI, config, upgrade, "
            "RSI collection) to AI assistants."
        ),
        epilog=(
            "Config file discovery order: --config_path argument > "
            "JUNOS_OPS_CONFIG env var > ./config.ini > "
            "~/.config/junos-ops/config.ini."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify config.ini is loadable and list routers, then exit.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="transport protocol (default: stdio)",
    )
    args = parser.parse_args()

    if args.check:
        sys.exit(_check_config())

    try:
        mcp.run(transport=args.transport)
    except KeyboardInterrupt:
        # Bypass normal interpreter shutdown: FastMCP's stdio reader runs in a
        # daemon thread blocked on sys.stdin, and joining it at shutdown can
        # crash with "_enter_buffered_busy" on Python 3.14.
        os._exit(0)


if __name__ == "__main__":
    main()
