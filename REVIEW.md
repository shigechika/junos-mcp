# Review rules for this repository

Review rules on top of the reviewer's default focus. Three things:
which findings are blocking here, which classes to report that the
default focus would otherwise skip, and which are noise. The reasoning
behind the rules lives in `.github/copilot-instructions.md` (its
numbered focus items are cited below) and `CLAUDE.md`, which the
reviewer also receives.

## Always blocking

- **Adding an authentication or permission error to
  `_RETRYABLE_CONNECT_ERRORS` (§4).** `pool.py`'s `_connect` retries
  only `ConnectError` / `ConnectTimeoutError`; `ConnectAuthError` is
  excluded on purpose, because retrying bad credentials risks account
  lockout on the device.
- **A new batch tool that wraps a `common.args`- or
  `common.config`-mutating operation inside `common.run_parallel`
  workers (§4).** Those are process-global and mutated per tool call,
  so running such an operation across threads is a silent cross-host
  race. `daily_brief` pre-populates the `host` option before spawning
  threads precisely to dodge a ConfigParser race of this kind.
- **Building a configuration mutation by string concatenation (§5).**
  Config writes go through `Config.load` / `dev.rpc.*` — `push_config`
  passes its set commands to PyEZ as a structured NETCONF
  `<load-configuration>` RPC. A new tool that assembles a config change
  as a free-text on-box `op` or shell string instead is the finding.
- **Hand-editing `server.json`'s `version` fields away from the `"0.0.0"`
  sentinel (§3)**, outside `release.yml`'s `mcp-registry` job. That job
  patches both fields from the git tag at publish time and
  `tests/test_version_consistency.py` asserts the committed sentinel;
  the single source of truth is `junos_mcp/__init__.py`'s
  `__version__`.
- **Broadening logging of a connection error path (§6).** This
  repository delegates credentials entirely to junos-ops, and the one
  credential-adjacent string it handles is the connect error text
  (`PoolConnectionError`, `__main__.py --check-host`'s
  `conn.get("error_message")`). A new debug log of the full `conn` dict,
  or of that text at wider scope, needs to be shown not to carry
  embedded auth detail.

## Report even though the default focus would not

- **Any change to lock scope, entry lifecycle or eviction timing in
  `pool.py` (§4)**, as advisory, even when the diff looks safe and its
  tests pass. `tests/test_pool.py` only calls `acquire()` sequentially
  on one thread, and `tests/test_server.py` mocks `common.run_parallel`
  out entirely — so **nothing in the suite exercises the pool under
  real threads**, while production concurrency reaches it through
  `run_show_command_batch` and `daily_brief`. A stale-connection-reuse
  or lock-ordering bug here would surface only under concurrent hosts.
  Say so rather than inferring safety from a green test diff.
- **Moving `_connect`'s retry `time.sleep` outside the per-host
  `entry.lock` (§4)**, as advisory unless the diff explains the intent
  it changes: holding the lock during the sleep blocks same-host
  callers only, never other hosts, and that is the point.
- **A new `@mcp.tool()`'s name and docstring (§5).** With 24 tools
  already registered, the calling model relies on them to pick the
  right one, so a vague name or a docstring missing a parameter
  constraint it would otherwise have to guess — `output_format`'s
  pipe-stage caveat, for instance — is a functional defect here. Report
  it even though docstring accuracy is normally out of scope when
  reviewing code.

## Never report

- `run_show_command(s)`'s `command` argument being an arbitrary CLI
  string forwarded to `show.run_cli()`. That is the tool's job, not a
  defect. Do not describe this path as safely structured either — it
  is not, and does not need to be. The reviewable surface here is
  `output_format` handling, and the config-mutation rule above.
- Anything CI already fails on, restated as a review comment. `ruff
  check .` is gated at a pinned version, and
  `tests/test_smoke_probes.py` already fails the build for a registered
  tool with no probe spec. This does **not** extend to that file's
  estate-specific-literal assertion — a hostname, model or address
  leaking into a public repository is worth catching twice.
- `ruff format` findings. Formatting is deliberately not gated here;
  see `ruff.toml`.
- Suggestions to hand-build an MCP content envelope
  (`{"content": [...], "isError": ...}`) inside a tool handler. FastMCP
  wraps returned values already.
- Suggestions to *replace* `release-please.yml`'s
  `secrets.RELEASE_PLEASE_TOKEN` with `GITHUB_TOKEN`. Preferring the
  dedicated token is deliberate: under GitHub's recursion-prevention
  rule a `GITHUB_TOKEN`-authored tag push does not trigger the
  downstream `release` workflow. (The line falls back to `GITHUB_TOKEN`
  when the secret is unset, so a finding about the fallback arm itself
  is still fair game.)
