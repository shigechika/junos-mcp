# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.15.1](https://github.com/shigechika/junos-mcp/compare/v0.15.0...v0.15.1) (2026-06-29)


### Bug Fixes

* line-level alarm filter so a clean node section can't hide another node's alarm ([#25](https://github.com/shigechika/junos-mcp/issues/25)) ([c679df4](https://github.com/shigechika/junos-mcp/commit/c679df438b0715ec7020e5dd8efbb8bcde952ecf)), closes [#21](https://github.com/shigechika/junos-mcp/issues/21)

## [0.15.0](https://github.com/shigechika/junos-mcp/compare/v0.14.0...v0.15.0) (2026-06-18)


### Features

* **daily_brief:** add RE-fault and route-baseline checks + crash isolation ([#18](https://github.com/shigechika/junos-mcp/issues/18)) ([e7fe7f5](https://github.com/shigechika/junos-mcp/commit/e7fe7f5451f55c3809c2aa26ecb79125717c8e48))
* **health_check:** add lightweight device-non-connecting health check tool ([#22](https://github.com/shigechika/junos-mcp/issues/22)) ([ea9520c](https://github.com/shigechika/junos-mcp/commit/ea9520cc6365963697fe0b22bac9af21053655b3))


### Bug Fixes

* **daily_brief:** exclude mgmt and internal logical units from IF_DOWN (closes [#13](https://github.com/shigechika/junos-mcp/issues/13)) ([#14](https://github.com/shigechika/junos-mcp/issues/14)) ([243c3ac](https://github.com/shigechika/junos-mcp/commit/243c3ac48fb870fee0da5e1e0a93d0416dcc202c))
* **daily_brief:** flag IF_DOWN only for described ports down within since_hours ([#16](https://github.com/shigechika/junos-mcp/issues/16)) ([407d066](https://github.com/shigechika/junos-mcp/commit/407d0665bd51359b6421746b147729f0f8b6d27f)), closes [#15](https://github.com/shigechika/junos-mcp/issues/15)
* **daily_brief:** skip RE redundancy check on SRX chassis clusters ([#20](https://github.com/shigechika/junos-mcp/issues/20)) ([d8600c4](https://github.com/shigechika/junos-mcp/commit/d8600c42ee7d8b75f68de28bfeb2d287b493af4f))

## [Unreleased]

### Added
- `daily_brief`: two new per-host checks (PR
  [#18](https://github.com/shigechika/junos-mcp/pull/18)) — `[RE_FAULT]`
  flags an explicit routing-engine fault state on dual-RE chassis (a healthy
  backup RE reporting Present/Backup does not page), and an optional
  `route_baseline` parameter flags `[ROUTE_BASELINE]` when the `inet.0`
  destination count deviates from the expected value (scope with tags, e.g.
  `tags=["main"], route_baseline=152`; routing-instance tables are ignored).
  Also hardens the result rendering against a crashed worker.

### Changed
- `daily_brief`: the interface-down check now uses `show interfaces
  descriptions` instead of `show interfaces terse`. A physical interface is
  flagged `[IF_DOWN]` only when it has a description (`Admin=up`, `Link=down`)
  **and** its `Last flapped` time is within `since_hours`. This suppresses two
  noise classes that survived [#13](https://github.com/shigechika/junos-mcp/issues/13):
  undescribed unused access ports (no description) and chronically-down ports
  (down for longer than the window). Real uplinks / inter-switch links carry
  descriptions, so a genuine recent failure is still caught. Reported lines
  now include the down-since timestamp. Closes
  [#15](https://github.com/shigechika/junos-mcp/issues/15).

### Fixed
- `daily_brief`: skip the RE redundancy check on SRX chassis clusters. PyEZ
  facts on a cluster report `2RE=True` with a self-contradictory RE0 status
  of "Absent" while that RE is master and up (observed on an SRX4600
  cluster), which raised a false `[RE_FAULT]` on a healthy cluster. A
  genuinely failed cluster node still surfaces through the chassis-alarms
  check. Closes [#19](https://github.com/shigechika/junos-mcp/issues/19).
- `daily_brief`: exclude management interfaces (`fxp`/`me`/`vme`/`em`) and
  internal logical units (`.16386`, `.32767`/`.32768`) from `IF_DOWN`
  reporting, in addition to loopback. These are cosmetically "up down" and
  were burying real anomalies on large fleets. Physical VC ports
  (`sxe`/`vcp`) are intentionally still reported. Closes
  [#13](https://github.com/shigechika/junos-mcp/issues/13).

## [0.14.0] - 2026-05-25

### Added
- `daily_brief`: new MCP tool for morning health checks across multiple
  devices in parallel (closes [#10](https://github.com/shigechika/junos-mcp/issues/10)).
  Runs four checks per host — system/chassis alarms, interfaces up/down
  (loopback excluded), and syslog alert-pattern matching within a
  configurable look-back window (`since_hours`, default 18 h).
  Watched syslog patterns: `RPD_BGP_NEIGHBOR_STATE_CHANGED` (away from
  Established), `ESWD_STP_PORT_ROLE_CHANGE`, `OSPF.*neighbor.*down`,
  `KERN_ARP_ADDR_CHANGE`, `IF_DOWN`.
  Output is a three-tier Markdown summary: CRITICAL (connection failure),
  WARNING (anomalies found), OK (clean).
  Accepts the same `hostnames` / `tags` / `max_workers` / `config_path`
  arguments as `run_show_command_batch`.

## [0.13.0] - 2026-05-25

### Added
- `install_package`: new `unlink` flag (`unlink=True`). Dispatches to
  `upgrade._install_via_cli_with_unlink()` which runs
  `request system software add <pkg> unlink` directly via CLI, bypassing
  PyEZ `SW.install()`. Use on low-flash devices (EX2300 / EX3400,
  ≈1.3 GB `/dev/gpt/junos`) where major version upgrades fail with
  *"ERROR: insufficient space"* because PyEZ does not expose the `unlink`
  parameter. Requires junos-ops ≥ 0.23.0.
- `push_config`: new `no_commit` flag (`no_commit=True`). Issues
  `commit confirmed` but intentionally skips the final commit so
  JUNOS auto-rolls back after `confirm_timeout` minutes. Health check
  is skipped. Useful for triggering service restarts that lack a
  `request ...restart` command (e.g. syslog daemon on EX3400 post-upgrade).
  Requires junos-ops ≥ 0.22.2.
- `check_reachability` / `check_remote_packages`: output table now includes
  an `avail` column showing available disk space on the `rpath` filesystem
  (MiB/GiB). Hosts below 600 MiB are marked `!`. Calls
  `upgrade.get_disk_avail()` (`get-system-storage-information` RPC)
  for each connected host. Requires junos-ops ≥ 0.20.0.
- `run_show_command` / `run_show_commands`: new `output_format` parameter
  (`"text"` default / `"json"` / `"xml"`). `json` and `xml` request
  structured output from the device via NETCONF. Note: JunOS drops CLI
  pipe stages (`| match`, `| last`, `| count`) under json/xml — use
  `"text"` when pipe filtering is needed. Implemented via
  `junos_ops.show.run_cli()` / `run_cli_batch()`. Requires junos-ops ≥ 0.18.0.

### Changed
- Dependency floor bumped from `>=0.16.9` to `>=0.23.0` to cover all
  four upstream API additions above.

## [0.12.0] - 2026-05-01

### Added
- `run_show_command_batch`: new optional `grep_pattern` parameter (Python `re`
  pattern). When set, only lines matching the pattern are kept from each host's
  output; header lines (`#`-prefixed) are always preserved; hosts with no match
  show `(no match)`. Reduces large batch outputs (e.g. 93 routers ×
  `show route summary`) from hundreds of KB to a few hundred bytes, enabling
  inline result handling without tool-results file I/O.
- Per-host NETCONF connection pool (`junos_mcp/pool.py`).  The pool reuses
  idle PyEZ `Device` connections across tool calls, serialising concurrent
  operations on the same host through a per-host `threading.Lock`.  Enabled
  by default; controlled by two environment variables:
  - `JUNOS_MCP_POOL=0` — disable the pool (each call opens a fresh connection,
    same as before this release).
  - `JUNOS_MCP_POOL_IDLE=<seconds>` — idle timeout in seconds (default `60`).
    A connection unused for longer than this is closed and reopened on the
    next call.  Set to `0` to disable idle eviction.

## [0.11.0] - 2026-04-16

### Changed
- `push_config` default health check is now `["uptime"]` (NETCONF
  `get-system-uptime-information` RPC) instead of
  `["ping count 3 255.255.255.255 rapid"]`. Matches the default fix
  landed in [junos-ops 0.16.8](https://github.com/shigechika/junos-ops/blob/main/CHANGELOG.md#0168---2026-04-16).
  `uptime` reuses the existing NETCONF session, so it reflects whether
  commit confirmed left the management plane reachable without
  depending on ICMP. The broadcast-ping default was triggering
  spurious auto-rollbacks on devices that block ICMP to
  `255.255.255.255` (SRX345 fleets among them). Explicit
  `health_check=[...]` callers are unaffected.
- Dependency floor raised from `>=0.16.7` to `>=0.16.9` to pull in
  this fix plus the `check_remote_package_by_model` stale-cache fix
  that transparently improves `check_remote_packages`.

## [0.10.0] - 2026-04-16

### Added
- Three new MCP tools wrapping the `junos-ops check` subcommand
  (closes [#4](https://github.com/shigechika/junos-mcp/issues/4)):
  - `check_reachability` — fast NETCONF reachability probe
    (`gather_facts=False`, 5 s TCP probe). Mirrors `junos-ops check --connect`.
  - `check_local_inventory` — verify local firmware checksums against the
    `config.ini` `<model>.file` / `<model>.hash` inventory. No device
    connection required. Mirrors `--local`.
  - `check_remote_packages` — verify the staged firmware checksum on each
    device. Doubles as post-SCP copy verification. Mirrors `--remote`.
- All three reuse `junos_ops.display.format_check_table` /
  `format_check_local_inventory` for rendering. Tag filter accepts the
  same AND/OR grammar as the other batch tools.

## [0.9.0] - 2026-04-16

### Fixed
- `get_router_list` and `run_show_command_batch` no longer fail with
  `AttributeError: module 'junos_ops.common' has no attribute '_filter_by_tags'`
  against junos-ops ≥ 0.16.6. Upstream renamed `_filter_by_tags(set)` to
  `_filter_by_tag_groups(list[set])` when making `--tags` repeatable with
  OR-between-groups semantics. Call sites switched to
  `common._parse_tag_groups(tags)` + `common._filter_by_tag_groups(groups)`.
  Dependency floor bumped from `>=0.16.0` to `>=0.16.7`.

### Changed
- MCP `tags` parameters now accept the same AND/OR grammar as the
  `junos-ops --tags` CLI flag: each list element is one tag group
  (comma-separated tags AND together within a group); multiple list
  elements OR together across groups. E.g.
  `["tokyo,core", "backup"]` means `(tokyo AND core) OR backup`.
  A plain `["backup"]` still behaves as before.

## [0.8.0] - 2026-04-15

### Fixed
- `push_config` no longer raises `TypeError` at runtime when resolving the
  health-check outcome. The tool subscripts `_run_health_check()`'s return
  value as `hc["ok"]` / `hc["message"]`, which required the dict shape
  introduced in junos-ops 0.16.0; against 0.14.1 through 0.15.0 the
  function still returned `bool` and the tool crashed. The dependency
  floor is bumped from `>=0.14.1` to `>=0.16.0` to match reality.

### Changed
- CI matrix now includes a `windows-latest` × Python 3.12 smoke-test job
  (closes [#2](https://github.com/shigechika/junos-mcp/issues/2)) to guard
  against stdio newline regressions in the upstream `mcp` package
  (cf. [python-sdk#2433](https://github.com/modelcontextprotocol/python-sdk/issues/2433)).
  `fail-fast: false` keeps Linux results visible when Windows trips on
  something unrelated.

## [0.7.0] - 2026-04-13

### Added
- `--check-host HOSTNAME` option to `python -m junos_mcp` — with `--check`,
  opens a NETCONF session to the given host to verify reachability and
  authentication in addition to config.ini loading.
- `CHANGELOG.md` (Keep a Changelog format).

### Changed
- `run_show_command_batch` signature reordered to `(command, hostnames=None,
  tags=None, ...)`. `command` is now a required positional; `hostnames` is
  optional (omit to target all config sections, or combine with `tags`).

## [0.6.0] - 2026-04-13

### Added
- Tag-based host filtering (closes #1): `get_router_list`,
  `run_show_command_batch`, and `collect_rsi_batch` accept an optional
  `tags: list[str]` argument. Hosts whose `tags = ...` in `config.ini` is a
  superset of the requested tags (AND-match) are selected.
- `-V` / `--version` CLI option to print the version and exit.
- `--check` CLI option to load `config.ini`, list routers, and exit
  (exit code 1 on error) — useful for smoke-testing before registering the
  server with an AI assistant.
- `KeyboardInterrupt` handling (`os._exit(0)`) to avoid the Python 3.14
  `_enter_buffered_busy` crash in the FastMCP stdio reader thread.

### Changed
- Console script entry point switched from `junos_mcp.server:mcp.run` to
  `junos_mcp.__main__:main` so `junos-mcp --version` / `--check` work when
  installed via `pip`.
- README sections refreshed: added CLI options, tag-based host filtering,
  and a rewritten architecture note reflecting that
  `contextlib.redirect_stdout` is no longer used (junos-ops ≥ 0.14.1).

## [0.5.2] - 2026-04-05

### Changed
- Switched to junos-ops 0.14.1 `format_*` rendering API; eliminated
  `contextlib.redirect_stdout` from every MCP tool.
- Hardened release workflow; pinned `server.json` version to a sentinel
  placeholder that CI overwrites from the git tag at release time.

[Unreleased]: https://github.com/shigechika/junos-mcp/compare/v0.14.0...HEAD
[0.14.0]: https://github.com/shigechika/junos-mcp/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/shigechika/junos-mcp/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/shigechika/junos-mcp/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/shigechika/junos-mcp/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/shigechika/junos-mcp/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/shigechika/junos-mcp/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/shigechika/junos-mcp/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/shigechika/junos-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/shigechika/junos-mcp/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/shigechika/junos-mcp/releases/tag/v0.5.2
