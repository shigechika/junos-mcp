"""Allow running as: python -m junos_mcp"""

from junos_mcp.server import mcp

mcp.run(transport="stdio")
