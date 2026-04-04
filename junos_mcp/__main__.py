"""Allow running as: python -m junos_mcp [--transport stdio|streamable-http]"""

import argparse

from junos_mcp.server import mcp

parser = argparse.ArgumentParser(description="junos-mcp MCP server")
parser.add_argument(
    "--transport",
    choices=["stdio", "streamable-http"],
    default="stdio",
    help="transport protocol (default: stdio)",
)
args = parser.parse_args()
mcp.run(transport=args.transport)
