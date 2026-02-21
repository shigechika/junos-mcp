# junos-ops-mcp

MCP (Model Context Protocol) server for [junos-ops](https://github.com/shigechika/junos-ops).

Exposes Juniper Networks device operations to MCP-compatible AI assistants
(Claude Desktop, Claude Code, etc.) via STDIO transport.

## Features

Read-only device operations (Phase 1):

| Tool | Description |
|------|-------------|
| `get_device_facts` | Get basic device information (model, hostname, serial, version) |
| `get_version` | Get JUNOS version with upgrade status |
| `run_show_command` | Run any CLI show command |
| `list_remote_files` | List files on remote device path |

## Requirements

- Python 3.12+
- [junos-ops](https://github.com/shigechika/junos-ops) with a valid `config.ini`
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) >= 1.0

## Installation

```bash
pip install junos-ops-mcp
```

Or for development:

```bash
git clone https://github.com/shigechika/junos-ops-mcp.git
cd junos-ops-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

## Configuration

This server uses the same `config.ini` as junos-ops. See [junos-ops README](https://github.com/shigechika/junos-ops) for details.

Each tool accepts an optional `config_path` parameter. If omitted, the default search order is used:
1. `./config.ini`
2. `~/.config/junos-ops/config.ini`

## Usage

### Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "junos-ops": {
      "command": "/path/to/junos-ops-mcp/.venv/bin/python",
      "args": ["-m", "junos_ops_mcp.server"]
    }
  }
}
```

### Claude Desktop

Add to Claude Desktop settings (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "junos-ops": {
      "command": "/path/to/junos-ops-mcp/.venv/bin/python",
      "args": ["-m", "junos_ops_mcp.server"]
    }
  }
}
```

### MCP Inspector (development)

```bash
mcp dev junos_ops_mcp/server.py
```

## Testing

```bash
pytest tests/ -v
```

## Architecture

### stdout Capture

junos-ops functions use `print()` for output. Since MCP STDIO transport uses stdout for JSON-RPC communication, all `print()` output is captured via `contextlib.redirect_stdout` and returned as tool results.

### Global State Initialization

junos-ops uses `common.args` and `common.config` as global variables. The MCP server initializes these using the same pattern as the test fixtures in junos-ops (`conftest.py`).

## License

Apache License 2.0
