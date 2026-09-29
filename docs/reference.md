# Reference

## Tool index

### Device information

| Tool | Description | Connection |
|---|---|:---:|
| `get_device_facts` | Basic device information (model, hostname, serial, version) | Yes |
| `get_version` | JUNOS version with upgrade status | Yes |
| `get_router_list` | List routers from `config.ini` (optionally filtered by tags) | No |
| `health_check` | Server version + config status (router count, distinct tags). Lightweight; does not connect to any device | No |

### CLI command execution

| Tool | Description | Connection |
|---|---|:---:|
| `run_show_command` | Run a single CLI show command (`output_format`: text/json/xml) | Yes |
| `run_show_commands` | Run multiple CLI commands in a single session (`output_format`: text/json/xml) | Yes |
| `run_show_command_batch` | Run a command on multiple devices in parallel (tag filter + `grep_pattern`) | Yes |

### Configuration management

| Tool | Description | Connection |
|---|---|:---:|
| `get_config` | Device configuration (text/set/xml format) | Yes |
| `get_config_diff` | Config diff against a rollback version | Yes |
| `push_config` | **Writes.** Push config with commit confirmed + health check | Yes |

### Upgrade operations

| Tool | Description | Connection |
|---|---|:---:|
| `check_upgrade_readiness` | Check if device is ready for upgrade | Yes |
| `compare_version` | Compare two JUNOS version strings | No |
| `get_package_info` | Model-specific package file and hash | No |
| `list_remote_files` | List files on remote device path | Yes |
| `copy_package` | **Writes.** Copy firmware package via SCP with checksum | Yes |
| `install_package` | **Writes.** Install firmware with pre-flight checks (`unlink` flag for EX2300/EX3400) | Yes |
| `rollback_package` | **Writes.** Roll back to previous package version | Yes |
| `schedule_reboot` | **Writes.** Schedule device reboot at a specified time | Yes |

### Diagnostics

| Tool | Description | Connection |
|---|---|:---:|
| `collect_rsi` | Collect RSI/SCF with model-specific timeouts | Yes |
| `collect_rsi_batch` | Collect RSI/SCF from multiple devices in parallel (tag filter) | Yes |

### Pre-flight checks

Equivalent to the `junos-ops check` subcommand modes. All three reuse the
junos-ops display layer for table rendering.

| Tool | Description | Connection |
|---|---|:---:|
| `check_reachability` | Probe NETCONF reachability + available disk space per host (fast: no facts, 5s TCP probe) | Yes |
| `check_local_inventory` | Verify local firmware checksums against `config.ini` inventory | No |
| `check_remote_packages` | Verify staged firmware checksum + available disk space on devices (post-SCP verification) | Yes |

### Daily operations

| Tool | Description | Connection |
|---|---|:---:|
| `daily_brief` | Morning health check across multiple devices in parallel — alarms, interface up/down, syslog alert patterns within a look-back window (`since_hours`, default 18 h), dual-RE faults (`[RE_FAULT]`; skipped on SRX chassis clusters, whose facts misreport RE status), and an optional `inet.0` route-count baseline (`route_baseline`). Returns a CRITICAL/WARNING/OK Markdown summary. | Yes |
| `daily_brief_start` / `daily_brief_result` | Run the same sweep as a background job for fleets too large for one call (a hosted client's per-call limit is about 60 s): `daily_brief_start` returns a `job_id` at once, poll `daily_brief_result` until `done`. The synchronous `daily_brief` now stops after `JUNOS_DEADLINE` seconds (default 45; 0 disables) and lists unfinished hosts under `NOT CHECKED`. | Yes |

See [Setup — Write operations](setup.md#write-operations) for what the five
writing tools actually call on the device and which `config.ini` privilege
gates each one.

## Structured output format

`run_show_command` and `run_show_commands` accept an optional `output_format`
parameter:

| Value | Description |
|---|---|
| `"text"` | Default. Plain-text CLI output (same as typing the command) |
| `"json"` | NETCONF JSON output — device returns a structured dict |
| `"xml"` | NETCONF XML output — device returns pretty-printed XML |

CLI pipe stages (`| match`, `| last`, `| count`, etc.) are silently
dropped regardless of `output_format`. PyEZ's `Device.cli()` sends the
command over NETCONF RPC, which JUNOS does not pipe-process. Run the command
without pipes and filter client-side instead, or use
`run_show_command_batch`'s `grep_pattern` (below) for server-side-style
filtering of a single command against one or many hosts.

```python
run_show_command("router-a", "show bgp summary", output_format="json")
```

## Server-side output filtering

`run_show_command_batch` accepts an optional `grep_pattern` argument (Python
`re` pattern). Only lines matching the pattern are kept from each host's
output; header lines (starting with `#`) are always preserved, and hosts with
no matching lines show `(no match)`. This is what turns a 93-router ×
`show route summary` batch into a few hundred bytes instead of hundreds of KB:

```python
run_show_command_batch(
    command="show route summary",
    tags=["main"],
    grep_pattern=r"inet\.0:\s+\d+ destinations",
)
```

Two constraints worth knowing before reaching for it: `run_show_command_batch`
has no `output_format` argument at all — output is always plain text, so
`grep_pattern` can't be combined with `json`/`xml` output the way
`run_show_command`/`run_show_commands` support — and it takes a single
`command` string per call, not a list.

## Tag-based host filtering

`run_show_command_batch`, `collect_rsi_batch`, and `get_router_list` accept
an optional `tags` argument. The grammar matches the `junos-ops --tags` CLI
flag:

- Each list element is **one tag group**. Comma-separated tags inside a group
  **AND** together.
- Multiple list elements **OR** together across groups.
- Combined with `hostnames` on batch tools, the result is the
  **intersection**. An empty intersection returns an error.

```python
# 1 group, 1 tag — hosts tagged "main"
run_show_command_batch(command="show route summary", tags=["main"])

# 1 group, 2 tags — AND within the group: tokyo AND edge
collect_rsi_batch(tags=["tokyo,edge"])

# 2 groups — OR across groups: main OR backup
get_router_list(tags=["main", "backup"])
```

See the [junos-ops tag documentation](https://github.com/shigechika/junos-ops#tag-based-host-filtering)
for how to tag sections in `config.ini`.

## Connection pool

junos-mcp maintains a per-host NETCONF connection pool. Reusing an idle
`Device` avoids the TCP/NETCONF handshake on every tool call; the pool
serialises concurrent operations on the same host through a per-host lock.

| Environment variable | Default | Description |
|---|---|---|
| `JUNOS_MCP_POOL` | `1` | Set to `0` to disable the pool and open a fresh connection per call |
| `JUNOS_MCP_POOL_IDLE` | `60` | Idle timeout (seconds); `0` disables eviction |
| `JUNOS_MCP_POOL_CONNECT_ATTEMPTS` | `2` | Connection attempts per pooled connect |
| `JUNOS_MCP_POOL_CONNECT_DELAY` | `1.0` | Delay (seconds) between pooled-connection retry attempts |

Pooled connections are long-lived SSH sessions. Where session duration is
restricted by policy, set `JUNOS_MCP_POOL_IDLE` shorter than the inactivity
limit, or set `JUNOS_MCP_POOL=0` to disable the pool entirely.

## CLI

```bash
python -m junos_mcp                       # start the MCP server (stdio, default)
python -m junos_mcp -V                    # print version and exit
python -m junos_mcp --check               # load config.ini, list routers, exit
python -m junos_mcp --check --check-host rt1   # also verify NETCONF auth against rt1
python -m junos_mcp --transport streamable-http  # HTTP transport (default: stdio)
```

`--check` exits `1` on error (config missing or unparsable). Combine with
`--check-host HOSTNAME` to confirm credentials actually authenticate against
a real device, not just that the inventory file parses.
