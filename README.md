# junos-mcp

English | [日本語](README.ja.md)

MCP (Model Context Protocol) server for [junos-ops](https://github.com/shigechika/junos-ops).

Exposes Juniper Networks device operations to MCP-compatible AI assistants
(Claude Desktop, Claude Code, etc.) via STDIO transport.
While [junos-ops](https://github.com/shigechika/junos-ops) is the CLI tool for humans,
**junos-mcp** is the AI-facing interface to the same powerful engine.

## Features

### Device Information

| Tool | Description | Connection |
|------|-------------|:----------:|
| `get_device_facts` | Get basic device information (model, hostname, serial, version) | Yes |
| `get_version` | Get JUNOS version with upgrade status | Yes |
| `get_router_list` | List all available routers from config.ini | No |

### CLI Command Execution

| Tool | Description | Connection |
|------|-------------|:----------:|
| `run_show_command` | Run a single CLI show command | Yes |
| `run_show_commands` | Run multiple CLI commands in a single session | Yes |
| `run_show_command_batch` | Run a command on multiple devices in parallel | Yes |

### Configuration Management

| Tool | Description | Connection |
|------|-------------|:----------:|
| `get_config` | Get device configuration (text/set/xml format) | Yes |
| `get_config_diff` | Show config diff against a rollback version | Yes |
| `push_config` | Push config with commit confirmed + health check | Yes |

### Upgrade Operations

| Tool | Description | Connection |
|------|-------------|:----------:|
| `check_upgrade_readiness` | Check if device is ready for upgrade | Yes |
| `compare_version` | Compare two JUNOS version strings | No |
| `get_package_info` | Get model-specific package file and hash | No |
| `list_remote_files` | List files on remote device path | Yes |
| `copy_package` | Copy firmware package via SCP with checksum | Yes |
| `install_package` | Install firmware with pre-flight checks | Yes |
| `rollback_package` | Rollback to previous package version | Yes |
| `schedule_reboot` | Schedule device reboot at specified time | Yes |

### Diagnostics

| Tool | Description | Connection |
|------|-------------|:----------:|
| `collect_rsi` | Collect RSI/SCF with model-specific timeouts | Yes |
| `collect_rsi_batch` | Collect RSI/SCF from multiple devices in parallel | Yes |

### Safety by Design

All destructive operations (`push_config`, `copy_package`, `install_package`,
`rollback_package`, `schedule_reboot`) default to **dry-run mode** (`dry_run=True`).
The AI assistant must explicitly set `dry_run=False` to make changes.

`push_config` provides additional safety features not found in other Junos MCP servers:

- **commit confirmed** with configurable timeout (auto-rollback if not confirmed)
- **Fallback health check** after commit (ping, NETCONF uptime probe, or any CLI command)
- **Automatic rollback** if health check fails (commit is not confirmed, timer expires)

## Requirements

- Python 3.12+
- [junos-ops](https://github.com/shigechika/junos-ops) with a valid `config.ini`
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) >= 1.0

## Installation

```bash
pip install junos-mcp
```

Or for development:

```bash
git clone https://github.com/shigechika/junos-mcp.git
cd junos-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

## Configuration

This server uses the same `config.ini` as junos-ops. See [junos-ops README](https://github.com/shigechika/junos-ops) for details.

Each tool accepts an optional `config_path` parameter. If omitted, the default search order is used:
1. Environment variable `JUNOS_OPS_CONFIG`
2. `./config.ini`
3. `~/.config/junos-ops/config.ini`

## Usage

### Claude Code

Register the MCP server with `claude mcp add`:

```bash
claude mcp add junos-mcp \
  -e JUNOS_OPS_CONFIG=~/.config/junos-ops/config.ini \
  -- python -m junos_mcp
```

The `--scope` (`-s`) option controls where the configuration is stored:

| Scope | Description | Config location |
|-------|-------------|-----------------|
| `local` (default) | Current project, current user only | `~/.claude.json` |
| `project` | Current project, shared with team | `.mcp.json` in project root |
| `user` | All projects, current user only | `~/.claude.json` |

### Claude Desktop

Add to Claude Desktop config file:

| OS | Config file |
|----|-------------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "junos-mcp": {
      "command": "python",
      "args": ["-m", "junos_mcp"],
      "env": {
        "JUNOS_OPS_CONFIG": "/path/to/config.ini"
      }
    }
  }
}
```

Restart Claude Desktop after editing.

### MCP Inspector (development)

```bash
mcp dev junos_mcp/server.py
```

## Testing

```bash
pytest tests/ -v
```

71 tests covering all 19 tools, helper functions, and edge cases.

## Architecture

### stdout Capture

junos-ops functions use `print()` for output. Since MCP STDIO transport uses stdout for JSON-RPC communication, all `print()` output is captured via `contextlib.redirect_stdout` and returned as tool results.

### Global State Initialization

junos-ops uses `common.args` and `common.config` as global variables. The MCP server initializes these using the same pattern as the test fixtures in junos-ops (`conftest.py`).

### Parallel Execution

Batch tools (`run_show_command_batch`, `collect_rsi_batch`) use `ThreadPoolExecutor` via junos-ops `common.run_parallel()` with configurable `max_workers`.

## License

Apache License 2.0
