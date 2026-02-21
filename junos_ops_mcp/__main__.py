"""Allow running as: python -m junos_ops_mcp"""

from junos_ops_mcp.server import mcp

mcp.run(transport="stdio")
