# Setup

## Requirements

- Python 3.12+
- [junos-ops](https://github.com/shigechika/junos-ops) with a valid `config.ini`
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) >= 1.0

## Install

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

This server uses the same `config.ini` as junos-ops — device hostnames,
per-host or `[DEFAULT]` SSH/NETCONF credentials, and tag assignments. See the
[junos-ops README](https://github.com/shigechika/junos-ops) for the file
format.

Each tool accepts an optional `config_path` parameter. If omitted, the
default search order is:

1. Environment variable `JUNOS_OPS_CONFIG`
2. `./config.ini`
3. `~/.config/junos-ops/config.ini`

`config.ini` is not optional in practice: every tool — including
`get_router_list` and `health_check`, which never open a device connection —
reads from it at startup. There is no degrade-gracefully path if it can't be
found, so put a working `config.ini` in one of the three locations above
before registering the server with any MCP client.

| Environment variable | Default | Description |
|---|---|---|
| `JUNOS_OPS_CONFIG` | unset | Path to `config.ini`; falls back to the search order above when unset |
| `JUNOS_MCP_POOL` | `1` | Set to `0` to disable the per-host NETCONF connection pool and open a fresh connection per call |
| `JUNOS_MCP_POOL_IDLE` | `60` | Idle timeout in seconds for pooled connections; `0` disables eviction |
| `JUNOS_MCP_POOL_CONNECT_ATTEMPTS` | `2` | Connection attempts per pooled connect |
| `JUNOS_MCP_POOL_CONNECT_DELAY` | `1.0` | Delay in seconds between pooled-connection retry attempts |

## Verify before wiring it into anything

```bash
python -m junos_mcp --check
python -m junos_mcp --check --check-host rt1
```

`--check` loads `config.ini` and lists the configured routers, exiting `1` on
error. Add `--check-host HOSTNAME` to also open a NETCONF session against
that device and confirm the credentials actually authenticate — `--check`
alone only proves the inventory file parses.

## Write operations

Five tools change device state. Everything else only reads. These are the
same five that default to `dry_run=True` — see the
[design notes on the Home page](index.md#design-notes) for the dry-run and
commit-confirmed mechanics; this table is about what each one calls and the
device-side privilege that gates it.

| Tool | API call | Permission gate |
|---|---|---|
| `push_config` | `jnpr.junos.utils.config.Config`: `lock` → `load(format="set")` → `diff` → `commit_check` → `commit(confirm=confirm_timeout)` → health check → final `commit` → `unlock` | The `config.ini` account for the target host needs a JUNOS login class permitting configuration mode and commit — not a read-only/operator class. The exact class name is whatever was provisioned per device in `config.ini`. |
| `copy_package` | `junos_ops.upgrade.copy()` — SCPs the firmware package to the device with checksum verification and pre-copy storage cleanup | Same account needs file-copy / storage-write access (SCP to device flash). |
| `install_package` | `junos_ops.upgrade.install()` — version check, pending-rollback check, copy + checksum, clear reboot schedule, rescue-config save, then PyEZ `SW.install()` (or `request system software add` via the `unlink` CLI path on low-flash EX2300/EX3400) | Requires software-installation privilege — JUNOS `maintenance`-class or superuser login class. |
| `rollback_package` | `junos_ops.upgrade.rollback()` — equivalent of `request system software rollback`, only after confirming a pending version exists | Same elevated software-maintenance privilege as `install_package`. |
| `schedule_reboot` | Schedules `request system reboot at <time>` | Requires reboot/maintenance privilege on the device. |

Provision the `config.ini` account for a host with a read-only/operator login
class and these five tools fail against that host with a permission error;
every other tool — show commands, config reads, diagnostics, `daily_brief` —
keeps working. There is no separate plugin-level switch for this: the
privilege boundary is entirely in the JUNOS login class assigned to the
account in `config.ini`.

## Register with an MCP client

### Claude Code (plugin)

This repository doubles as a single-plugin marketplace:

```
/plugin marketplace add shigechika/junos-mcp
/plugin install junos-mcp@junos-mcp
```

The plugin launches `uvx junos-mcp` and reads the same [environment
variables](#configuration) as every other transport. Export
`JUNOS_OPS_CONFIG` (or drop `config.ini` at `./config.ini` or
`~/.config/junos-ops/config.ini`) before starting Claude Code — see
[Configuration](#configuration) above for why this file isn't optional.

`uvx` must be on the `PATH` of the process that runs Claude Code — a login
shell usually has it, but a GUI-launched app may not; install
[uv](https://docs.astral.sh/uv/) system-wide if the plugin fails to start.

### Claude Code (manual)

```bash
claude mcp add junos-mcp \
  -e JUNOS_OPS_CONFIG=~/.config/junos-ops/config.ini \
  -- python -m junos_mcp
```

The `--scope` (`-s`) option controls where the configuration is stored:

| Scope | Description | Config location |
|---|---|---|
| `local` (default) | Current project, current user only | `~/.claude.json` |
| `project` | Current project, shared with team | `.mcp.json` in project root |
| `user` | All projects, current user only | `~/.claude.json` |

### Claude Desktop

`claude_desktop_config.json`:

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

### Remote access (OAuth via mcp-stdio)

junos-mcp also supports Streamable HTTP transport, for remote access through
[mcp-stdio](https://github.com/shigechika/mcp-stdio) as an OAuth proxy:

```bash
# on the remote server
JUNOS_OPS_CONFIG=~/.config/junos-ops/config.ini \
  python -m junos_mcp --transport streamable-http

# on the local machine
claude mcp add junos-mcp -- mcp-stdio https://your-server:8000/mcp
```

See the ["Remote Access with OAuth (via mcp-stdio)" section of the README](https://github.com/shigechika/junos-mcp/blob/main/README.md)
for the full walkthrough and OAuth provider setup.

## Next

[Reference](reference.md) covers every tool, output formats, tag-based host
filtering, the connection pool, and the CLI.
