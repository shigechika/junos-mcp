# junos-mcp

MCP server for [junos-ops](https://github.com/shigechika/junos-ops).

Exposes Juniper Networks device operations to MCP-compatible AI assistants
(Claude Desktop, Claude Code, etc.) via STDIO transport. While junos-ops is
the CLI tool for humans, **junos-mcp** is the AI-facing interface to the same
engine — CLI show commands, config diffs and pushes, firmware upgrades,
RSI/SCF diagnostics, and a `daily_brief` health check across a fleet.

## Tools by area

| Area | Tools |
|---|---|
| Device information | `get_device_facts`, `get_version`, `get_router_list`, `health_check` |
| CLI command execution | `run_show_command`, `run_show_commands`, `run_show_command_batch` |
| Configuration management | `get_config`, `get_config_diff`, `push_config` |
| Upgrade operations | `check_upgrade_readiness`, `compare_version`, `get_package_info`, `list_remote_files`, `copy_package`, `install_package`, `rollback_package`, `schedule_reboot` |
| Diagnostics | `collect_rsi`, `collect_rsi_batch` |
| Pre-flight checks | `check_reachability`, `check_local_inventory`, `check_remote_packages` |
| Daily operations | `daily_brief` |

**Five tools write to a device:** `push_config`, `copy_package`,
`install_package`, `rollback_package`, `schedule_reboot`. All five default to
dry-run. Everything else only reads. See
[Setup](setup.md#write-operations) for what each write tool actually calls
and which config.ini credential gates it.

## Design notes

**Dry-run first, and that choice is load-bearing.** The five write tools
above default to `dry_run=True`; the caller must explicitly set
`dry_run=False` to make a change. `push_config` goes further, issuing JUNOS
`commit confirmed` with a configurable timeout and a post-commit health check
(ping, NETCONF uptime probe, or any CLI command) that automatically rolls
back the change if the device doesn't come back healthy.

**Stdout stays JSON-RPC-only.** Since junos-ops 0.14.1, core functions return
structured `dict` values and never print to stdout; MCP tools render output
through `junos_ops.display.format_*()` instead. No
`contextlib.redirect_stdout` wrapper is needed, so the MCP STDIO channel
never gets a stray `print()` mixed into a JSON-RPC frame.

**Batch tools shrink output before it reaches the model.**
`run_show_command_batch` accepts a server-side `grep_pattern` and tag-based
host filtering, so a query against dozens of devices — e.g. `inet.0` route
counts across a fleet — comes back as a few hundred bytes instead of hundreds
of KB of raw CLI text.

## Next steps

- [Setup](setup.md) — installation, `config.ini`, environment variables,
  registering the server, and the write-tool privilege gates
- [Reference](reference.md) — every tool, output formats, tag filtering,
  the connection pool, and the CLI
