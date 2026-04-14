# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/shigechika/junos-mcp/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/shigechika/junos-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/shigechika/junos-mcp/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/shigechika/junos-mcp/releases/tag/v0.5.2
