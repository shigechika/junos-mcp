"""MCP server exposing junos-ops device operations.

Provides 22 tools for Juniper Networks device management: device info,
CLI execution, config management, firmware upgrade, RSI/SCF collection,
and pre-flight checks.

Supports STDIO and Streamable HTTP transports.

As of junos-mcp 0.11.0 this server uses junos-ops ≥ 0.16.9, which
guarantees that every core function (including ``_run_health_check``)
returns a structured ``dict`` and does not print to stdout. MCP tools
build responses by calling the core function and then
``junos_ops.display.format_*(result)`` to get rendered text as a
string — no ``contextlib.redirect_stdout`` is needed anywhere in this
module, so the MCP STDIO JSON-RPC channel is safe by construction.
"""

import argparse
import os
import os.path
import re
from pprint import pformat

from lxml import etree
from mcp.server.fastmcp import FastMCP

from jnpr.junos.utils.config import Config
from junos_ops import common
from junos_ops import display
from junos_ops import rsi
from junos_ops import upgrade

mcp = FastMCP("junos-mcp")


def _resolve_config_path(config_path: str) -> str:
    """Resolve config file path from argument, environment variable, or default.

    Priority: config_path argument > JUNOS_OPS_CONFIG env var > default search.
    """
    if config_path:
        return os.path.expanduser(config_path)
    env_path = os.environ.get("JUNOS_OPS_CONFIG", "")
    if env_path:
        return os.path.expanduser(env_path)
    return common.get_default_config()


def _init_globals(config_path: str = "") -> str | None:
    """Initialize junos-ops global state (``common.args`` and ``common.config``).

    :param config_path: Path to config.ini. Empty string uses env var or default search.
    :return: Error message string, or None on success.
    """
    # argparse Namespace shaped to match cli.main()'s post-parse state.
    # ``subcommand`` is set per-tool before invoking firmware operations
    # (copy/install/rollback/reboot) so install() picks the right branch.
    common.args = argparse.Namespace(
        debug=False,
        dry_run=False,
        force=False,
        config=_resolve_config_path(config_path),
        list_format=None,
        rebootat=None,
        configfile=None,
        confirm_timeout=1,
        health_check=None,
        no_health_check=False,
        no_confirm=False,
        show_command=None,
        showfile=None,
        retry=0,
        rpc_timeout=None,
        tags=None,
        specialhosts=[],
        subcommand=None,
    )

    # read_config() returns a dict in junos-ops 0.14.0+.
    cfg_result = common.read_config()
    if not cfg_result["ok"]:
        return f"Config error: {cfg_result['error']}"
    return None


def _ensure_config(config_path: str) -> str | None:
    """Initialize globals if needed. Return error string or None."""
    # 既に初期化済みで同じ config の場合はスキップ
    if (
        common.config is not None
        and common.args is not None
        and (not config_path or common.args.config == config_path)
    ):
        return None
    return _init_globals(config_path)


def _connect_and_run(hostname: str, config_path: str, operation):
    """Connect to device and run operation, returning formatted result.

    :param hostname: Target hostname (must exist in config.ini).
    :param config_path: Path to config.ini.
    :param operation: Callable(hostname, dev) -> str. Receives connected device.
    :return: Result string for MCP tool response.
    """
    err = _ensure_config(config_path)
    if err:
        return err

    # hostname が config に存在するか確認
    if not common.config.has_section(hostname):
        return f"Error: hostname '{hostname}' not found in config"

    # host キーが未設定なら section 名を使う
    if not common.config.has_option(hostname, "host") or common.config.get(hostname, "host") is None:
        common.config.set(hostname, "host", hostname)

    # connect() returns a dict in junos-ops 0.14.0+.
    conn = common.connect(hostname)
    if not conn["ok"]:
        msg = conn.get("error_message") or conn.get("error") or "Connection failed"
        return f"Connection error: {msg}"
    dev = conn["dev"]

    try:
        return operation(hostname, dev)
    finally:
        try:
            dev.close()
        except Exception:
            pass


@mcp.tool()
def get_device_facts(hostname: str, config_path: str = "") -> str:
    """Get basic device information (model, hostname, serial, version, etc.).

    Args:
        hostname: Target device hostname (must exist in config.ini)
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        return f"# {hostname}\n{pformat(dev.facts)}"

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def get_version(hostname: str, config_path: str = "") -> str:
    """Get JUNOS version information with upgrade status.

    Shows running version, planning version, pending version,
    local/remote package status, and reboot schedule.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        result = upgrade.show_version(hostname, dev)
        return f"# {hostname}\n{display.format_version(result)}"

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def run_show_command(hostname: str, command: str, config_path: str = "") -> str:
    """Run a CLI show command on the device and return output.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        command: CLI command to execute (e.g., "show bgp summary")
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        try:
            output = dev.cli(command)
            return f"# {hostname}\n## {command}\n{output.strip()}"
        except Exception as e:
            return f"# {hostname}\nError running '{command}': {e}"

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def run_show_commands(hostname: str, commands: list[str], config_path: str = "") -> str:
    """Run multiple CLI show commands on the device in a single session.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        commands: List of CLI commands to execute
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        lines = []
        for cmd in commands:
            try:
                output = dev.cli(cmd)
                lines.append(f"## {cmd}\n{output.strip()}")
            except Exception as e:
                lines.append(f"## {cmd}\nError: {e}")
        return f"# {hostname}\n" + "\n\n".join(lines)

    return _connect_and_run(hostname, config_path, _operation)


def _resolve_hostnames(
    hostnames: list[str] | None,
    tags: list[str] | None,
) -> list[str] | str:
    """Resolve target hostnames from explicit list and/or tag filter.

    Returns a list of hostnames, or an error string. Both inputs are
    optional; if both are empty, all config sections are returned.
    When tags are given with hostnames, results are the intersection.
    """
    all_sections = common.config.sections()
    if tags:
        tag_groups = common._parse_tag_groups(tags)
        matched = common._filter_by_tag_groups(tag_groups)
        if hostnames:
            hset = set(hostnames)
            resolved = [h for h in matched if h in hset]
        else:
            resolved = matched
        if not resolved:
            return f"Error: no hosts match tags {tags}"
        return resolved
    if hostnames:
        missing = [h for h in hostnames if h not in all_sections]
        if missing:
            return f"Error: hostnames not found in config: {missing}"
        return list(hostnames)
    return list(all_sections)


@mcp.tool()
def run_show_command_batch(
    command: str,
    hostnames: list[str] | None = None,
    tags: list[str] | None = None,
    grep_pattern: str | None = None,
    max_workers: int = 5,
    config_path: str = "",
) -> str:
    """Run a CLI show command on multiple devices in parallel.

    Uses ThreadPoolExecutor for concurrent execution. Either ``hostnames``
    or ``tags`` selects the targets; if both are omitted, every router in
    config.ini is targeted. When both are given, the intersection is used.

    Args:
        command: CLI command to execute on all devices
        hostnames: List of target device hostnames (must exist in config.ini)
        tags: Tag filter. Each list element is one tag group (comma-separated
            tags AND together within a group). Multiple list elements OR
            together across groups. E.g. ``["tokyo,core", "backup"]`` means
            ``(tokyo AND core) OR backup``. Combined with ``hostnames`` the
            result is the intersection.
        grep_pattern: Optional Python ``re`` pattern. When set, only lines
            matching the pattern (via ``re.search``) are kept from each
            host's output. Header lines (starting with ``#``) are always
            preserved. Hosts with no matching lines show ``(no match)``.
            Reduces large batch outputs to the essential lines.
        max_workers: Maximum parallel threads (default 5)
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err

    compiled = None
    if grep_pattern:
        try:
            compiled = re.compile(grep_pattern)
        except re.error as exc:
            return f"Error: invalid grep_pattern: {exc}"

    targets = _resolve_hostnames(hostnames, tags)
    if isinstance(targets, str):
        return targets

    def _run_one(hostname):
        output = run_show_command(hostname, command, config_path)
        if compiled is None:
            return output
        # 接続エラーやコマンドエラーはヘッダーなし — フィルタせずそのまま返す
        if not output.startswith("#"):
            return output
        lines = []
        has_match = False
        for line in output.splitlines():
            if line.startswith("#"):
                lines.append(line)
            elif compiled.search(line):
                lines.append(line)
                has_match = True
        if not has_match:
            lines.append("(no match)")
        return "\n".join(lines)

    results = common.run_parallel(_run_one, targets, max_workers=max_workers)
    parts = [results[h] for h in targets if h in results]
    return "\n\n".join(parts)


@mcp.tool()
def list_remote_files(hostname: str, config_path: str = "") -> str:
    """List files on the remote device path (/var/tmp by default).

    Args:
        hostname: Target device hostname (must exist in config.ini)
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        # list_format controls short vs long rendering in format_list_remote.
        original_format = common.args.list_format
        common.args.list_format = "long"
        try:
            result = upgrade.list_remote_path(hostname, dev)
            return f"# {hostname}\n{display.format_list_remote(result)}"
        finally:
            common.args.list_format = original_format

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def check_upgrade_readiness(hostname: str, config_path: str = "") -> str:
    """Check if a device is ready for upgrade.

    Verifies whether the device is already running the target version,
    and performs a dry-run to check local/remote package availability.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        # check_running_package() returns a dict with a ``match`` field.
        running = upgrade.check_running_package(hostname, dev)
        if running["match"]:
            return (
                f"# {hostname}\n"
                f"Device is already running the target version "
                f"({running['running']} matches {running['expected_file']})."
            )
        # dry_run() returns a structured dict; format_dry_run renders it.
        result = upgrade.dry_run(hostname, dev)
        status = "READY" if result["ok"] else "NOT READY"
        return (
            f"# {hostname}\nUpgrade readiness: {status}\n"
            f"{display.format_dry_run(result)}"
        )

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def compare_version(left: str, right: str) -> str:
    """Compare two JUNOS version strings.

    Returns whether left is greater than, equal to, or less than right.
    No device connection required.

    Args:
        left: First JUNOS version string (e.g., "22.4R3-S6.5")
        right: Second JUNOS version string (e.g., "23.2R1.0")
    """
    result = upgrade.compare_version(left, right)
    if result is None:
        return "Error: invalid version string (None)"
    labels = {1: f"{left} > {right}", 0: f"{left} == {right}", -1: f"{left} < {right}"}
    return labels.get(result, f"Error: unexpected result {result}")


@mcp.tool()
def get_router_list(tags: list[str] | None = None, config_path: str = "") -> str:
    """List routers defined in config.ini, optionally filtered by tags.

    Returns section names from config.ini, which represent the hostnames
    that can be used with other tools. No device connection required.

    Args:
        tags: Tag filter. Each list element is one tag group (comma-separated
            tags AND together within a group); multiple list elements OR
            together across groups. E.g. ``["tokyo,core", "backup"]`` means
            ``(tokyo AND core) OR backup``. None/empty returns all.
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err
    if tags:
        tag_groups = common._parse_tag_groups(tags)
        sections = common._filter_by_tag_groups(tag_groups)
        label = f"Routers matching tags {tags}"
    else:
        sections = common.config.sections()
        label = "Available routers"
    if not sections:
        return "No routers defined in config" if not tags else f"{label}: (none)"
    return f"{label} ({len(sections)}):\n" + "\n".join(
        f"- {s}" for s in sections
    )


@mcp.tool()
def get_package_info(hostname: str, model: str, config_path: str = "") -> str:
    """Get package file name and expected hash for a specific device model.

    Retrieves model-specific package information from config.ini.
    No device connection required.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        model: Device model name (e.g., "EX2300-24T")
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err
    if not common.config.has_section(hostname):
        return f"Error: hostname '{hostname}' not found in config"
    try:
        file = upgrade.get_model_file(hostname, model)
        hash_val = upgrade.get_model_hash(hostname, model)
        return f"# {hostname} ({model})\nPackage file: {file}\nExpected hash: {hash_val}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_config(hostname: str, output_format: str = "text", config_path: str = "") -> str:
    """Get device configuration.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        output_format: Output format - "text" (default), "set", or "xml"
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        try:
            options = {"format": output_format}
            config = dev.rpc.get_config(options=options)
            if output_format == "xml":
                return f"# {hostname}\n{etree.tostring(config, pretty_print=True).decode()}"
            return f"# {hostname}\n{config.text.strip()}"
        except Exception as e:
            return f"# {hostname}\nError getting config: {e}"

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def get_config_diff(hostname: str, rollback_id: int = 1, config_path: str = "") -> str:
    """Show configuration difference compared to a rollback version.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        rollback_id: Rollback version to compare against (0-49, default 1)
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        try:
            cu = Config(dev)
            cu.rollback(rollback_id)
            diff = cu.diff()
            cu.rollback(0)  # 元に戻す
            if diff is None:
                return f"# {hostname}\nNo differences from rollback {rollback_id}"
            return f"# {hostname}\n## diff vs rollback {rollback_id}\n{diff}"
        except Exception as e:
            return f"# {hostname}\nError getting config diff: {e}"

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def collect_rsi(
    hostname: str,
    output_dir: str = "",
    config_path: str = "",
) -> str:
    """Collect RSI (Request Support Information) and SCF (Show Configuration) from a device.

    Saves two files: {hostname}.SCF (show configuration) and {hostname}.RSI
    (request support information). Model-specific timeouts are applied
    automatically (e.g., SRX Branch: 1200s, Virtual Chassis: 1800s).

    Args:
        hostname: Target device hostname (must exist in config.ini)
        output_dir: Directory to save output files (empty uses config RSI_DIR or current dir)
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err
    if not common.config.has_section(hostname):
        return f"Error: hostname '{hostname}' not found in config"

    # If the caller supplied an output_dir, override the config value
    # for this one call so ``rsi.collect_rsi`` picks it up.
    original_rsi_dir = None
    had_rsi_dir = common.config.has_option(hostname, "RSI_DIR")
    if output_dir:
        if had_rsi_dir:
            original_rsi_dir = common.config.get(hostname, "RSI_DIR")
        save_dir = os.path.expanduser(output_dir)
        if not save_dir.endswith("/"):
            save_dir += "/"
        common.config.set(hostname, "RSI_DIR", save_dir)

    # host キーが未設定なら section 名を使う
    if not common.config.has_option(hostname, "host") or common.config.get(hostname, "host") is None:
        common.config.set(hostname, "host", hostname)

    conn = common.connect(hostname)
    if not conn["ok"]:
        msg = conn.get("error_message") or conn.get("error") or "Connection failed"
        return f"Connection error: {msg}"
    dev = conn["dev"]

    try:
        # rsi.collect_rsi() is the dict-returning core added in
        # junos-ops 0.14.0. It writes the SCF and RSI files and
        # returns a structured result with paths and byte counts.
        # We keep a locally-shaped summary here (including byte counts
        # and error messages) rather than using display.format_rsi,
        # which is tailored for the CLI and does not include the
        # byte counts/full error messages useful for an MCP response.
        result = rsi.collect_rsi(hostname, dev)

        lines = []
        if result.get("scf"):
            lines.append(
                f"SCF saved: {result['scf']['path']} "
                f"({result['scf']['bytes']} bytes)"
            )
        elif result.get("error") == "scf":
            lines.append(f"SCF failed: {result.get('error_message')}")
        if result.get("rsi"):
            lines.append(
                f"RSI saved: {result['rsi']['path']} "
                f"({result['rsi']['bytes']} bytes)"
            )
        elif result.get("error") in ("rsi_rpc", "rsi_write"):
            lines.append(f"RSI failed: {result.get('error_message')}")

        return f"# {hostname}\n" + "\n".join(lines)
    finally:
        try:
            dev.close()
        except Exception:
            pass
        # Restore the original RSI_DIR to avoid leaking per-call overrides.
        if output_dir:
            if original_rsi_dir is not None:
                common.config.set(hostname, "RSI_DIR", original_rsi_dir)
            elif not had_rsi_dir:
                common.config.remove_option(hostname, "RSI_DIR")


@mcp.tool()
def collect_rsi_batch(
    hostnames: list[str] | None = None,
    tags: list[str] | None = None,
    output_dir: str = "",
    max_workers: int = 20,
    config_path: str = "",
) -> str:
    """Collect RSI/SCF from multiple devices in parallel.

    Uses ThreadPoolExecutor for concurrent collection. Default 20 workers
    matches junos-ops CLI default for RSI collection. Either ``hostnames``
    or ``tags`` selects the targets; if both are omitted, every router in
    config.ini is targeted. When both are given, the intersection is used.

    Args:
        hostnames: List of target device hostnames
        tags: Tag filter. Each list element is one tag group (comma-separated
            tags AND together within a group); multiple list elements OR
            together across groups. Combined with ``hostnames`` the result is
            the intersection.
        output_dir: Directory to save output files (empty uses config RSI_DIR or current dir)
        max_workers: Maximum parallel threads (default 20)
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err

    targets = _resolve_hostnames(hostnames, tags)
    if isinstance(targets, str):
        return targets

    def _run_one(hostname):
        return collect_rsi(hostname, output_dir, config_path)

    results = common.run_parallel(_run_one, targets, max_workers=max_workers)
    parts = [results[h] for h in targets if h in results]
    return "\n\n".join(parts)


def _resolve_check_model(hostname: str, explicit: str | None) -> tuple[str | None, str | None]:
    """Resolve check model in junos-ops priority order: arg > config.ini > None.

    Device-facts fallback is intentionally skipped here; per-host check
    tools that connect can fetch it themselves via ``dev.facts``. Returns
    ``(model, source)`` where ``source`` is ``"cli"`` / ``"config"`` /
    ``None``.
    """
    if explicit:
        return explicit, "cli"
    try:
        cfg_model = common.config.get(hostname, "model")
        if cfg_model:
            return cfg_model, "config"
    except Exception:
        pass
    return None, None


def _check_one_host(hostname: str, do_connect: bool, do_remote: bool, explicit_model: str | None) -> dict:
    """Per-host check worker mirroring junos-ops cli._check_host.

    Re-implemented here (rather than calling the underscore-prefixed CLI
    helper) to avoid an extra coupling point. Uses the public
    ``upgrade.check_remote_package_by_model`` and ``common.connect``
    APIs with ``gather_facts=False`` + ``auto_probe=5`` for speed.
    """
    model, source = _resolve_check_model(hostname, explicit_model)
    row: dict = {
        "hostname": hostname,
        "model": model,
        "model_source": source,
        "connect": None,
        "remote": None,
    }
    if not (do_connect or do_remote):
        return row

    need_facts = do_remote and model is None
    conn = common.connect(hostname, gather_facts=need_facts, auto_probe=5)
    if not conn["ok"]:
        row["connect"] = {
            "ok": False,
            "message": conn.get("error_message") or "connect failed",
            "error": conn.get("error"),
        }
        return row

    dev = conn["dev"]
    row["connect"] = {"ok": True, "message": "connected", "error": None}
    try:
        if do_remote and model is None:
            try:
                facts_model = dev.facts.get("model") if need_facts else None
                if facts_model:
                    model = facts_model
                    source = "device"
                else:
                    elem = dev.rpc.get_software_information().find(".//product-model")
                    if elem is not None and elem.text:
                        model = elem.text
                        source = "device"
            except Exception:
                pass
            row["model"] = model
            row["model_source"] = source

        if do_remote:
            if model:
                try:
                    row["remote"] = upgrade.check_remote_package_by_model(hostname, dev, model)
                except Exception as e:
                    row["remote"] = {
                        "status": "unchecked",
                        "message": f"recipe lookup failed: {e}",
                        "file": None,
                        "cached": False,
                        "error": type(e).__name__,
                    }
            else:
                row["remote"] = {
                    "status": "unchecked",
                    "message": "model unknown",
                    "file": None,
                    "cached": False,
                }
    finally:
        try:
            dev.close()
        except Exception:
            pass
    return row


@mcp.tool()
def check_reachability(
    hostnames: list[str] | None = None,
    tags: list[str] | None = None,
    max_workers: int = 20,
    config_path: str = "",
) -> str:
    """Probe NETCONF reachability for one or more devices.

    Equivalent to ``junos-ops check --connect``. Opens a fast NETCONF
    handshake (no full PyEZ facts gathering, 5-second TCP probe) and
    reports per-host status as a table.

    Args:
        hostnames: List of target device hostnames (must exist in config.ini)
        tags: Tag filter. Each list element is one tag group (comma-separated
            tags AND together within a group); multiple list elements OR
            together across groups. Combined with ``hostnames`` the result is
            the intersection.
        max_workers: Maximum parallel threads (default 20, matches junos-ops)
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err
    targets = _resolve_hostnames(hostnames, tags)
    if isinstance(targets, str):
        return targets

    def _run_one(hostname):
        return _check_one_host(hostname, do_connect=True, do_remote=False, explicit_model=None)

    results = common.run_parallel(_run_one, targets, max_workers=max_workers)
    rows = [results[h] for h in targets if h in results]
    return display.format_check_table(rows, show_connect=True, show_local=False, show_remote=False)


@mcp.tool()
def check_local_inventory(model: str = "", config_path: str = "") -> str:
    """Verify local firmware checksums against the config.ini inventory.

    Equivalent to ``junos-ops check --local``. Iterates every
    ``<model>.file`` / ``<model>.hash`` pair in the DEFAULT section of
    ``config.ini`` and verifies the file on the staging server. No
    device connection required.

    Args:
        model: Restrict to a single model (empty = all configured models)
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err
    models = [model] if model else upgrade.iter_configured_models()
    rows: list[dict] = []
    for m in models:
        try:
            r = upgrade.check_local_package_by_model("DEFAULT", m)
            rows.append({
                "model": m,
                "file": r.get("file"),
                "local_file": r.get("local_file"),
                "status": r.get("status"),
                "cached": r.get("cached"),
                "actual_hash": r.get("actual_hash"),
                "expected_hash": r.get("expected_hash"),
                "message": r.get("message"),
                "error": r.get("error"),
            })
        except Exception as e:
            rows.append({
                "model": m,
                "file": None,
                "local_file": None,
                "status": "error",
                "cached": False,
                "actual_hash": None,
                "expected_hash": None,
                "message": f"config lookup failed: {e}",
                "error": type(e).__name__,
            })
    if not rows:
        return "No models with <model>.file entries found in config.ini DEFAULT section."
    return display.format_check_local_inventory(rows)


@mcp.tool()
def check_remote_packages(
    hostnames: list[str] | None = None,
    tags: list[str] | None = None,
    model: str = "",
    max_workers: int = 20,
    config_path: str = "",
) -> str:
    """Verify the staged firmware checksum on one or more devices.

    Equivalent to ``junos-ops check --remote``. Connects to each device
    via NETCONF and verifies the package file (``<model>.file``) sitting
    on the device against ``<model>.hash``. Doubles as post-SCP copy
    verification. Per-host model resolution: ``model`` arg > config.ini
    ``[host].model`` > device facts.

    Args:
        hostnames: List of target device hostnames (must exist in config.ini)
        tags: Tag filter. Each list element is one tag group (comma-separated
            tags AND together within a group); multiple list elements OR
            together across groups. Combined with ``hostnames`` the result is
            the intersection.
        model: Override model resolution for all hosts (empty = per-host resolution)
        max_workers: Maximum parallel threads (default 20)
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err
    targets = _resolve_hostnames(hostnames, tags)
    if isinstance(targets, str):
        return targets

    explicit = model or None

    def _run_one(hostname):
        return _check_one_host(hostname, do_connect=True, do_remote=True, explicit_model=explicit)

    results = common.run_parallel(_run_one, targets, max_workers=max_workers)
    rows = [results[h] for h in targets if h in results]
    return display.format_check_table(rows, show_connect=True, show_local=False, show_remote=True)


@mcp.tool()
def push_config(
    hostname: str,
    config_file: str = "",
    set_commands: list[str] | None = None,
    dry_run: bool = True,
    confirm_timeout: int = 1,
    health_check: list[str] | None = None,
    config_path: str = "",
) -> str:
    """Push configuration to a device with commit confirmed and health check.

    Supports two input methods (exactly one required):
    - config_file: Path to a .set or .j2 file containing set commands
    - set_commands: List of set command strings (inline)

    Safety features (not available in Juniper's official MCP server):
    - dry_run mode (default True): shows diff without committing
    - commit confirmed: auto-rollback if not confirmed within timeout
    - health check: auto-rollback on connectivity failure after commit

    Commit flow:
        lock -> load -> diff -> commit_check -> commit confirmed ->
        health check -> confirm -> unlock

    Args:
        hostname: Target device hostname (must exist in config.ini)
        config_file: Path to .set or .j2 file (mutually exclusive with set_commands)
        set_commands: List of set commands (mutually exclusive with config_file)
        dry_run: If True (default), show diff only without committing
        confirm_timeout: Minutes before auto-rollback (default 1, used with commit confirmed)
        health_check: Fallback health check commands tried in order after commit.
            Passes if ANY command succeeds. Supports "ping ..." (checks packets received),
            "uptime" (NETCONF RPC probe), or any CLI command (success if no exception).
            Default: ["uptime"] — uses the existing NETCONF session and does not
            depend on ICMP reachability. (Changed from broadcast ping in junos-mcp
            0.11.0 to match junos-ops 0.16.8+.)
        config_path: Path to config.ini (empty string uses default search)
    """
    # 入力バリデーション
    if config_file and set_commands:
        return "Error: specify either config_file or set_commands, not both"
    if not config_file and not set_commands:
        return "Error: specify config_file or set_commands"

    def _operation(hostname, dev):
        cu = Config(dev)

        # config ロック取得
        try:
            cu.lock()
        except Exception as e:
            return f"# {hostname}\nConfig lock failed: {e}"

        try:
            # set コマンドの準備
            if config_file:
                if config_file.endswith(".j2"):
                    commands = common.render_template(config_file, hostname, dev)
                else:
                    commands = common.load_commands(config_file)
            else:
                commands = set_commands

            cu.load("\n".join(commands), format="set")

            # 差分確認
            diff = cu.diff()
            if diff is None:
                cu.unlock()
                return f"# {hostname}\nNo changes to apply"

            # テンプレート使用時はレンダリング結果も表示
            rendered = ""
            if config_file and config_file.endswith(".j2"):
                rendered = "\n## Rendered commands\n" + "\n".join(
                    f"  {cmd}" for cmd in commands
                ) + "\n"

            # dry_run: diff 表示のみ
            if dry_run:
                result = f"# {hostname} (dry-run)\n{rendered}## Config diff\n{diff}"
                cu.rollback()
                cu.unlock()
                return result

            # validation
            cu.commit_check()

            # commit confirmed（自動ロールバック付き）
            cu.commit(confirm=confirm_timeout)

            # ヘルスチェック
            # junos-ops 0.16.0+: _run_health_check returns a structured
            # dict (ok / passed_command / commands / steps / message).
            # The ``message`` field carries the same multi-line text
            # the pre-0.16 bool-returning version used to print.
            # Default is "uptime" (NETCONF RPC) to match junos-ops 0.16.8+.
            # Broadcast ping used to be the default but fails on devices
            # that block ICMP to 255.255.255.255 (e.g. SRX345), causing
            # spurious auto-rollbacks.
            health_cmds = health_check if health_check is not None else ["uptime"]
            hc = upgrade._run_health_check(hostname, dev, health_cmds)

            if not hc["ok"]:
                # ヘルスチェック失敗 — confirm せずタイマー満了で自動ロールバック
                cu.unlock()
                return (
                    f"# {hostname}\n## Config diff\n{diff}\n"
                    f"## HEALTH CHECK FAILED\n{hc['message']}\n"
                    f"Config will auto-rollback in {confirm_timeout} minute(s).\n"
                    f"DO NOT run commit confirm manually."
                )

            # 確定（タイマー解除）
            cu.commit()
            cu.unlock()
            return (
                f"# {hostname}\n{rendered}## Config diff\n{diff}\n"
                f"## Result\nCommit confirmed and permanent."
            )

        except Exception as e:
            try:
                cu.rollback()
            except Exception:
                pass
            try:
                cu.unlock()
            except Exception:
                pass
            return f"# {hostname}\nConfig push failed: {e}"

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def copy_package(hostname: str, dry_run: bool = True, force: bool = False, config_path: str = "") -> str:
    """Copy firmware package to remote device via SCP with checksum verification.

    Checks if copy is needed (already running target version, or package
    already present on device). Cleans up storage before copying.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        dry_run: If True (default), show what would be done without copying
        force: If True, skip version checks and force copy
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        common.args.dry_run = dry_run
        common.args.force = force
        common.args.subcommand = "copy"
        result = upgrade.copy(hostname, dev)
        status = "OK" if result.get("ok") else "FAILED"
        if result.get("skipped"):
            status = f"SKIPPED ({result.get('skip_reason')})"
        return (
            f"# {hostname}\n## copy_package: {status}\n"
            f"{display.format_copy(result)}"
        )

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def install_package(hostname: str, dry_run: bool = True, force: bool = False, config_path: str = "") -> str:
    """Install firmware package on device with pre-flight checks.

    Full upgrade flow: version check -> rollback pending if needed ->
    copy (with checksum) -> clear reboot schedule -> rescue config save ->
    request system software add (with validation).

    Args:
        hostname: Target device hostname (must exist in config.ini)
        dry_run: If True (default), show what would be done without installing
        force: If True, skip version checks and force install
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        common.args.dry_run = dry_run
        common.args.force = force
        # install() branches on ``subcommand`` to decide whether to
        # skip the pre-install remote package check (upgrade) or
        # fail-fast when the remote package is missing (install-only).
        # MCP always drives the full copy+install pipeline, so "upgrade".
        common.args.subcommand = "upgrade"
        result = upgrade.install(hostname, dev)
        status = "OK" if result.get("ok") else "FAILED"
        if result.get("skipped"):
            status = f"SKIPPED ({result.get('skip_reason')})"
        return (
            f"# {hostname}\n## install_package: {status}\n"
            f"{display.format_install(result)}"
        )

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def rollback_package(hostname: str, dry_run: bool = True, config_path: str = "") -> str:
    """Rollback to previously installed package version.

    Checks pending version first. If no pending version exists, rollback is skipped.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        dry_run: If True (default), show what would be done without rolling back
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        common.args.dry_run = dry_run
        common.args.subcommand = "rollback"
        # get_pending_version still returns a plain string or None.
        pending = upgrade.get_pending_version(hostname, dev)
        if pending is None:
            return f"# {hostname}\nNo pending version, rollback skipped"

        result = upgrade.rollback(hostname, dev)
        status = "OK" if result.get("ok") else "FAILED"
        return (
            f"# {hostname}\n"
            f"Pending version: {pending}\n"
            f"## rollback_package: {status}\n"
            f"{display.format_rollback(result)}"
        )

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def schedule_reboot(
    hostname: str,
    reboot_at: str,
    dry_run: bool = True,
    force: bool = False,
    config_path: str = "",
) -> str:
    """Schedule device reboot at a specified time.

    Checks for existing reboot schedules. If one exists and force is False,
    the existing schedule is preserved.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        reboot_at: Reboot time in YYMMDDHHMM format (e.g., "2601020304" = 2026-01-02 03:04)
        dry_run: If True (default), show what would be done without scheduling
        force: If True, clear existing reboot schedule and set new one
        config_path: Path to config.ini (empty string uses default search)
    """
    # 日時パース
    try:
        reboot_dt = upgrade.yymmddhhmm_type(reboot_at)
    except argparse.ArgumentTypeError as e:
        return f"Error: {e}"

    def _operation(hostname, dev):
        common.args.dry_run = dry_run
        common.args.force = force
        common.args.subcommand = "reboot"
        result = upgrade.reboot(hostname, dev, reboot_dt)
        # reboot() preserves legacy exit codes in result["code"]
        # (0 success; 2..6 various failure modes).
        status = "OK" if result.get("ok") else f"FAILED (code={result.get('code')})"
        return (
            f"# {hostname}\n## schedule_reboot: {status}\n"
            f"{display.format_reboot(result)}"
        )

    return _connect_and_run(hostname, config_path, _operation)


if __name__ == "__main__":
    import argparse as _ap

    _parser = _ap.ArgumentParser()
    _parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    _args = _parser.parse_args()
    mcp.run(transport=_args.transport)
