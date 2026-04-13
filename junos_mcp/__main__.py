"""Allow running as: python -m junos_mcp"""

from __future__ import annotations

import argparse
import os
import sys

from junos_mcp import __version__
from junos_mcp.server import _ensure_config, mcp


def _check_config(check_host: str | None = None) -> int:
    """Verify config.ini is loadable and list available routers.

    If ``check_host`` is given, additionally open a NETCONF session to that
    host to verify reachability and authentication.
    """
    err = _ensure_config("")
    if err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1
    from junos_ops import common

    sections = common.config.sections()
    print(f"OK: config loaded from {common.args.config}")
    print(f"Routers ({len(sections)}): {', '.join(sections) if sections else '(none)'}")

    if check_host is None:
        return 0

    if not common.config.has_section(check_host):
        print(
            f"Error: host '{check_host}' not found in config",
            file=sys.stderr,
        )
        return 2
    if (
        not common.config.has_option(check_host, "host")
        or common.config.get(check_host, "host") is None
    ):
        common.config.set(check_host, "host", check_host)

    conn = common.connect(check_host)
    if not conn["ok"]:
        msg = conn.get("error_message") or conn.get("error") or "Connection failed"
        print(f"Connection error ({check_host}): {msg}", file=sys.stderr)
        return 2
    try:
        dev = conn["dev"]
        model = dev.facts.get("model", "?")
        version = dev.facts.get("version", "?")
        print(f"OK: connected to {check_host} (model={model}, version={version})")
    finally:
        try:
            conn["dev"].close()
        except Exception:
            pass
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
        "--check-host",
        metavar="HOSTNAME",
        help=(
            "With --check, also open a NETCONF session to HOSTNAME to verify "
            "reachability and authentication."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="transport protocol (default: stdio)",
    )
    args = parser.parse_args()

    if args.check or args.check_host:
        sys.exit(_check_config(args.check_host))

    try:
        mcp.run(transport=args.transport)
    except KeyboardInterrupt:
        # Bypass normal interpreter shutdown: FastMCP's stdio reader runs in a
        # daemon thread blocked on sys.stdin, and joining it at shutdown can
        # crash with "_enter_buffered_busy" on Python 3.14.
        os._exit(0)


if __name__ == "__main__":
    main()
