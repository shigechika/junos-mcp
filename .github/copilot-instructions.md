# Repository overview

`junos-mcp` is an MCP (Model Context Protocol) server that exposes
[junos-ops](https://github.com/shigechika/junos-ops) (a Juniper JUNOS CLI
tool/library) to AI assistants over **stdio** (default) or
**streamable-http** transport. Built on the official `mcp` Python SDK's
`FastMCP` (`junos_mcp/server.py`), with per-host NETCONF connection pooling
in `junos_mcp/pool.py`.

See `CLAUDE.md` (Japanese) for the authoritative module/tool inventory and
design notes — read it before reviewing changes to `server.py` or `pool.py`.
Note it currently says "23 tools" and "115 tests"; the actual counts have
drifted upward as tools were added, so don't treat those numbers in CLAUDE.md
as exact — check `grep -c '^@mcp.tool' junos_mcp/server.py` and the test run
output instead of citing a stale figure in review comments.

# Build & validate

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
pytest tests/ -v
```

This mirrors `.github/workflows/test.yml` (matrix over Python 3.12–3.14 on
Linux, plus one Windows 3.12 job specifically to guard against stdio
newline regressions, `modelcontextprotocol/python-sdk#2433`). There is no
`ruff`/`black`/`mypy` job in CI — don't hold diffs to a style standard this
repo hasn't opted into.

# What to focus review on in this repo

## 1. stdio transport: stdout is the JSON-RPC channel

`server.py`'s module docstring documents the actual mechanism: junos-ops
core functions (≥0.16.9) return structured `dict`s and never print; MCP
tools render text via `junos_ops.display.format_*(result)`, which *return*
a string rather than printing one. No `contextlib.redirect_stdout` exists
anywhere in the module — it isn't needed because nothing on this path calls
`print()`. Flag any new tool code in `server.py` that calls `print()`,
writes to stdout directly, or lets a dependency's default logging fall
through to stdout.

One legitimate exception: `junos_mcp/__main__.py`'s `--check`/`--check-host`
path does call `print()`, but it `sys.exit()`s before `mcp.run()` is ever
reached, so it never shares the stdio channel with a live JSON-RPC session.
Don't flag those.

## 2. FastMCP already wraps tool returns — don't ask for manual envelope code

`@mcp.tool()`-decorated functions return a plain `str` or `dict`; FastMCP's
`func_metadata.convert_result()` wraps it into `TextContent` (or structured
content) automatically. `health_check()` returning a plain `dict` is the
clean example — don't suggest a tool hand-build
`{"content": [...], "isError": ...}`.

The prevailing error-handling convention here is the *opposite* of
"let it raise": most tools catch exceptions internally and return a
human-readable error string, e.g. `push_config`'s
`f"# {hostname}\nConfig push failed: {e}"`, or `health_check`, which is
documented (PR #22) to "catch all exceptions and never raise." Don't flag
that pattern itself. Do flag a new tool whose broad `except Exception`
swallows the failure and returns `None`/an empty/success-shaped result with
no visible error text — that hides a real failure from both the model and
the operator, unlike the existing examples which always surface `hc["message"]`
or the exception text.

## 3. `server.json` version is a sentinel, not a real value

`server.json`'s top-level `version` and `packages[0].version` are pinned to
the placeholder `"0.0.0"`. `.github/workflows/release.yml`'s `mcp-registry`
job patches both fields from the git tag (`jq ... server.json`) at publish
time, and `tests/test_version_consistency.py` asserts the committed file
still carries the sentinel. Flag any diff that hand-edits `server.json`'s
version fields to something other than `"0.0.0"` outside of that release
job — it will fail `test_version_consistency.py` and defeats the point of
having a single source of truth (`junos_mcp/__init__.py`'s `__version__`).

## 4. Connection pooling (`pool.py`) — scrutinize touches carefully

`ConnectionPool` keys entries by `(hostname, config_path)` and holds a
per-entry `threading.Lock` for the full duration of an operation (checkout
to checkin), evicting on idle timeout (`JUNOS_MCP_POOL_IDLE`, default 60s)
or a disconnected device. `JUNOS_MCP_POOL=0` disables pooling entirely.

`tests/test_pool.py` verifies per-host isolation
(`test_different_hosts_get_different_entries`) and eviction logic, but only
by calling `acquire()` sequentially in a single thread — there is no test
that actually races threads against the same or different pool entries. In
production, real concurrency comes from `run_show_command_batch` and
`collect_rsi_batch`, which fan out via `common.run_parallel`
(`ThreadPoolExecutor`) — and `tests/test_server.py` mocks
`common.run_parallel` out entirely, so it never exercises the pool under
real threads either. Treat any change to lock scope, entry lifecycle, or
eviction timing in `pool.py` as needing more scrutiny than the test diff
alone shows, since a stale-connection-reuse or lock-ordering bug here would
most plausibly surface only when multiple hosts are hit concurrently.

## 5. Tool inputs are LLM-driven — treat them as adversarial, but be precise about the surface

- `push_config`'s `set_commands`/rendered `.j2` output is joined and passed
  to PyEZ as `cu.load("\n".join(commands), format="set")` — a structured
  NETCONF `<load-configuration>` RPC, not a shell/CLI string. New config-writing
  code should go through `Config.load`/`dev.rpc.*` the same way, not build a
  raw on-box `op`/shell command by string concatenation.
- `run_show_command(s)`'s `command` argument is, by design, an arbitrary CLI
  string forwarded to `show.run_cli()` — that's the tool's job, not a bug to
  flag. Don't claim this path is "safely structured"; it isn't, and doesn't
  need to be. Focus review here on `output_format` handling and on any new
  tool that builds a *config* mutation by concatenating a `run_show_command`-style
  free-text string instead of using the `Config`/RPC path above.
- A new `@mcp.tool()`'s name and docstring are what the calling model uses
  to pick and invoke it — with 24 tools already registered, flag a vague
  name or a docstring missing parameter constraints an LLM would otherwise
  have to guess (e.g. `output_format`'s pipe-stage caveat, or `tags`'
  AND/OR group syntax in `run_show_command_batch`).

## 6. Device credentials

junos-mcp itself never touches usernames/passwords directly — connection
setup is delegated to junos-ops' `common.connect()`/`config.ini`. The one
place a credential-adjacent string surfaces in this repo is the connection
error path (`PoolConnectionError`, and `__main__.py --check-host`'s
`conn.get("error_message")`), which forwards whatever the underlying
connect call reports. Flag any diff that adds logging of that error text at
a broader scope (e.g. a new debug log of the full `conn` dict) without
checking it can't include embedded auth details.

# Out of scope for review comments

- Formatting/style nits: no `ruff`/`black`/`mypy` step exists in this
  repo's CI.
- `release-please.yml` using `secrets.RELEASE_PLEASE_TOKEN` (falling back to
  `GITHUB_TOKEN`) instead of just `GITHUB_TOKEN` is intentional — a
  `GITHUB_TOKEN`-authored tag push doesn't trigger the downstream `release`
  workflow (GitHub's recursion-prevention rule), so this isn't a
  simplification opportunity.
