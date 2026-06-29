"""MCP server exposing junos-ops device operations.

Provides 23 tools for Juniper Networks device management: device info,
CLI execution, config management, firmware upgrade, RSI/SCF collection,
pre-flight checks, and daily operations.

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
import datetime
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
from junos_ops import show
from junos_ops import upgrade

from junos_mcp.pool import PoolConnectionError, get_pool

_SYSLOG_ALERT_RE = re.compile(
    r"RPD_BGP_NEIGHBOR_STATE_CHANGED.*Established->"
    r"|ESWD_STP_PORT_ROLE_CHANGE"
    r"|OSPF.*neighbor.*down"
    r"|KERN_ARP_ADDR_CHANGE"
    r"|IF_DOWN",
    re.IGNORECASE,
)
# Interfaces excluded from IF_DOWN reporting: loopback, management
# (fxp/me/vme/em), and internal logical units (.16386 internal IFL,
# .32767/.32768 virtual-chassis/internal) that are cosmetically "up down".
# Physical VC ports (sxe/vcp) are intentionally NOT skipped: a down VC member
# link can be a genuine fault.
_IF_DOWN_SKIP_PREFIX = ("lo", "fxp", "me", "vme", "em")
_IF_DOWN_SKIP_SUFFIX = (".16386", ".32767", ".32768")
# Absolute timestamp in "show interfaces <if>":
# "Last flapped   : 2026-06-05 14:00:00 JST (1w0d 02:00 ago)"
_LAST_FLAPPED_RE = re.compile(
    r"Last flapped\s*:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)
_SYSLOG_MAX_MATCHES = 10
# RE <status> strings that indicate a genuine fault. A healthy backup RE may
# report Present/Online/Backup (platform/version dependent), so page only on one
# of these explicit fault states rather than on the mere absence of "OK".
_RE_FAULT_STATES = {"fault", "fail", "failed", "offline", "absent", "empty", "testing"}
# "inet.0: 152 destinations, ..." — the ^ anchor (re.MULTILINE) keeps
# routing-instance tables ("VRF.inet.0:", "mgmt_junos.inet.0:") out.
_ROUTE_INET0_RE = re.compile(r"^inet\.0:\s+(\d+) destinations", re.MULTILINE)

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
        no_commit=False,
        unlink=False,
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

    Uses the per-host connection pool when JUNOS_MCP_POOL != 0 (the
    default).  The pool reuses an idle Device across calls, serialising
    concurrent operations for the same host through a per-host lock.
    Set JUNOS_MCP_POOL=0 to fall back to the original open-close-per-call
    behaviour.

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

    pool = get_pool()
    if pool is not None:
        norm_path = _resolve_config_path(config_path)
        try:
            with pool.acquire(hostname, norm_path) as dev:
                return operation(hostname, dev)
        except PoolConnectionError as e:
            return f"Connection error: {e}"

    # Pool disabled — original open-close-per-call path.
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
def run_show_command(
    hostname: str,
    command: str,
    output_format: str = "text",
    config_path: str = "",
) -> str:
    """Run a CLI show command on the device and return output.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        command: CLI command to execute (e.g., "show bgp summary")
        output_format: Output format — "text" (default), "json", or "xml".
            Note: JunOS drops pipe stages (| match, | last, | count) under
            json/xml; use "text" when pipe filtering is needed.
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        result = show.run_cli(dev, command, output_format=output_format, hostname=hostname)
        return display.format_show(result)

    return _connect_and_run(hostname, config_path, _operation)


@mcp.tool()
def run_show_commands(
    hostname: str,
    commands: list[str],
    output_format: str = "text",
    config_path: str = "",
) -> str:
    """Run multiple CLI show commands on the device in a single session.

    Commands are executed in sequence and stop on the first failure.
    To run all commands regardless of individual errors, call
    run_show_command once per command instead.

    Args:
        hostname: Target device hostname (must exist in config.ini)
        commands: List of CLI commands to execute
        output_format: Output format — "text" (default), "json", or "xml".
            Note: JunOS drops pipe stages (| match, | last, | count) under
            json/xml; use "text" when pipe filtering is needed.
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        result = show.run_cli_batch(dev, commands, output_format=output_format, hostname=hostname)
        return display.format_show(result)

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
        output = run_show_command(hostname, command, config_path=config_path)
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

        try:
            row["disk"] = upgrade.get_disk_avail(hostname, dev)
        except Exception:
            row["disk"] = None
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
    return display.format_check_table(rows, show_connect=True, show_local=False, show_remote=False, show_disk=True)


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
    return display.format_check_table(rows, show_connect=True, show_local=False, show_remote=True, show_disk=True)


@mcp.tool()
def push_config(
    hostname: str,
    config_file: str = "",
    set_commands: list[str] | None = None,
    dry_run: bool = True,
    confirm_timeout: int = 1,
    no_commit: bool = False,
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

    Commit flow (normal):
        lock -> load -> diff -> commit_check -> commit confirmed ->
        health check -> confirm -> unlock

    Commit flow (no_commit=True — intentional auto-rollback):
        lock -> load -> diff -> commit_check -> commit confirmed -> unlock
        (health check and final confirm are skipped; JUNOS rolls back
        automatically after confirm_timeout minutes)

    Args:
        hostname: Target device hostname (must exist in config.ini)
        config_file: Path to .set or .j2 file (mutually exclusive with set_commands)
        set_commands: List of set commands (mutually exclusive with config_file)
        dry_run: If True (default), show diff only without committing
        confirm_timeout: Minutes before auto-rollback (default 1, used with commit confirmed)
        no_commit: If True, issue commit confirmed but intentionally skip the final
            commit so JUNOS auto-rolls back after confirm_timeout minutes. Useful
            for triggering service restarts (e.g. syslog on EX3400) where no
            ``request ...restart`` command exists. dry_run=True takes precedence
            over no_commit (diff is shown but nothing is committed).
        health_check: Fallback health check commands tried in order after commit.
            Passes if ANY command succeeds. Supports "ping ..." (checks packets received),
            "uptime" (NETCONF RPC probe), or any CLI command (success if no exception).
            Default: ["uptime"] — uses the existing NETCONF session and does not
            depend on ICMP reachability. (Changed from broadcast ping in junos-mcp
            0.11.0 to match junos-ops 0.16.8+.)
            Ignored when no_commit=True.
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

            if no_commit:
                # 意図的な自動ロールバック — ヘルスチェック・確定なし
                cu.commit(confirm=confirm_timeout)
                cu.unlock()
                return (
                    f"# {hostname}\n{rendered}## Config diff\n{diff}\n"
                    f"## Result\nCommit confirmed {confirm_timeout} min applied"
                    f" — intentional auto-rollback (no_commit=True).\n"
                    f"DO NOT run commit confirm manually."
                )

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
def install_package(
    hostname: str,
    dry_run: bool = True,
    force: bool = False,
    unlink: bool = False,
    config_path: str = "",
) -> str:
    """Install firmware package on device with pre-flight checks.

    Full upgrade flow: version check -> rollback pending if needed ->
    copy (with checksum) -> clear reboot schedule -> rescue config save ->
    request system software add (with validation).

    Args:
        hostname: Target device hostname (must exist in config.ini)
        dry_run: If True (default), show what would be done without installing
        force: If True, skip version checks and force install
        unlink: If True, run ``request system software add <pkg> unlink``
            via CLI instead of PyEZ SW.install(). Use for low-flash devices
            (EX2300 / EX3400, ~1.3 GB /dev/gpt/junos) where major version
            upgrades fail with "ERROR: insufficient space" because PyEZ does
            not expose the unlink parameter. The CLI path frees ~330 MB by
            unlinking the source tgz during extraction.
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        common.args.dry_run = dry_run
        common.args.force = force
        common.args.unlink = unlink
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


def _syslog_line_dt(line: str) -> "datetime.datetime | None":
    """Parse the leading timestamp of a JunOS syslog line.

    JunOS syslog format: ``MMM DD HH:MM:SS[.mmm] hostname ...``
    No year is embedded; infer it by checking if the parsed date is in the
    future (year rollover at Jan 1).  Returns None on any parse failure.
    """
    parts = line.split(None, 3)
    if len(parts) < 3:
        return None
    try:
        month = datetime.datetime.strptime(parts[0], "%b").month
        day = int(parts[1])
        time_str = parts[2].split(".")[0]
        h, m, s = time_str.split(":")
        now = datetime.datetime.now()
        dt = datetime.datetime(now.year, month, day, int(h), int(m), int(s))
        if dt > now + datetime.timedelta(hours=1):
            dt = dt.replace(year=now.year - 1)
        return dt
    except Exception:
        return None


def _iface_last_flapped(dev, iface: str) -> "datetime.datetime | None":
    """Return the ``Last flapped`` time of an interface, or None.

    Parses the absolute timestamp from ``show interfaces <iface>``
    (``Last flapped   : 2026-06-05 14:00:00 JST (...)``).  Returns None when
    the field is absent (e.g. ``Never``) or unparseable.  The timestamp is
    naive local time; JunOS prints it in the device's configured time zone,
    which is assumed to match the server running this check.
    """
    try:
        out = dev.cli(f"show interfaces {iface}", warning=False)
    except Exception:
        return None
    m = _LAST_FLAPPED_RE.search(out)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _alarm_lines(out: str) -> list[str]:
    """Extract real alarm rows from ``show system/chassis alarms`` output.

    Junos sections multi-node output (SRX chassis cluster, Virtual Chassis)
    per node, so a whole-output ``"No alarms" not in out`` gate lets one
    node's "No alarms currently active" line suppress another node's real
    alarm — a silent failure on a partially-degraded cluster (issue #21).

    Filter line by line instead so single-chassis, cluster, and VC output
    are handled uniformly. The following are dropped as noise; whatever
    remains is a genuine alarm row:

      - the per-node "No alarms currently active" line,
      - ``nodeN:`` section headers,
      - ``----`` separator rules,
      - the "Alarm time ..." column header,
      - the "N alarms currently active" counter line.
    """
    lines: list[str] = []
    for raw in out.splitlines():
        s = raw.strip()
        if not s:
            continue
        if "no alarms currently active" in s.lower():
            continue
        if re.match(r"node\d+:?$", s, re.IGNORECASE):
            continue
        if re.match(r"-{3,}$", s):
            continue
        if s.lower().startswith("alarm time"):
            continue
        if re.match(r"\d+ alarms? currently active", s):
            continue
        lines.append(s)
    return lines


def _check_host_health(hostname: str, dev, since_hours: int, route_baseline: int = 0) -> dict:
    """Run health checks on a single connected device.

    Checks: system & chassis alarms, IF_DOWN, syslog alert patterns, dual-RE
    redundancy ([RE_FAULT]; skipped on SRX chassis clusters whose facts
    misreport RE status), and — when ``route_baseline`` > 0 — an inet.0
    destination count that deviates from the expected value ([ROUTE_BASELINE]).

    Returns a dict with keys:
      - ``hostname``: str
      - ``anomalies``: list[str] — tagged anomaly lines
      - ``error``: None (always None when this function is called; connection
        errors are caught in the caller)
    """
    anomalies: list[str] = []

    # 1. System alarms
    try:
        out = dev.cli("show system alarms", warning=False)
        for ln in _alarm_lines(out)[:5]:
            anomalies.append(f"[ALARM] {ln[:100]}")
    except Exception as exc:
        anomalies.append(f"[CHECK_ERROR] system alarms: {exc}")

    # 2. Chassis alarms
    try:
        out = dev.cli("show chassis alarms", warning=False)
        for ln in _alarm_lines(out)[:5]:
            anomalies.append(f"[CHASSIS_ALARM] {ln[:100]}")
    except Exception as exc:
        anomalies.append(f"[CHECK_ERROR] chassis alarms: {exc}")

    # 3. Interface down — report only described, admin-up ports whose link
    #    went down within the window (issue #15).  ``show interfaces
    #    descriptions`` lists only interfaces that have a description (= meant
    #    to be in use); undescribed unused ports never appear.  ``Last
    #    flapped`` filters out chronically-down ports, leaving genuine recent
    #    failures (uplinks / inter-switch links carry descriptions).
    try:
        out = dev.cli("show interfaces descriptions", warning=False)
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=since_hours)
        for line in out.splitlines():
            parts = line.split(None, 3)
            # Columns: Interface  Admin  Link  Description
            if len(parts) < 4 or parts[0] == "Interface":
                continue
            iface, admin, link = parts[0], parts[1].lower(), parts[2].lower()
            if admin != "up" or link != "down":
                continue
            if iface.startswith(_IF_DOWN_SKIP_PREFIX) or iface.endswith(
                _IF_DOWN_SKIP_SUFFIX
            ):
                continue
            flapped = _iface_last_flapped(dev, iface)
            if flapped is None:
                # Link down but flap time unknown — report conservatively.
                anomalies.append(f"[IF_DOWN] {iface} (described, link down)")
            elif flapped >= cutoff:
                anomalies.append(
                    f"[IF_DOWN] {iface} (down since {flapped:%Y-%m-%d %H:%M})"
                )
            # else: down longer than since_hours — chronic, suppressed
    except Exception as exc:
        anomalies.append(f"[CHECK_ERROR] interfaces descriptions: {exc}")

    # 4. Syslog alert patterns within the time window
    try:
        out = dev.cli("show log messages | last 200", warning=False)
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=since_hours)
        count = 0
        for line in out.splitlines():
            if count >= _SYSLOG_MAX_MATCHES:
                anomalies.append(f"[SYSLOG] ... ({count}+ matches, truncated)")
                break
            dt = _syslog_line_dt(line)
            if dt is None or dt < cutoff:
                continue
            if _SYSLOG_ALERT_RE.search(line):
                anomalies.append(f"[SYSLOG] {line[:120]}")
                count += 1
    except Exception as exc:
        anomalies.append(f"[CHECK_ERROR] syslog: {exc}")

    # 5. Routing-engine redundancy (dual-RE chassis): flag an explicit RE fault.
    #    Scope: only the first chassis's flat RE0/RE1 facts are inspected; a
    #    fault on a second Virtual Chassis member (carried in the re_info fact,
    #    not RE0/RE1) is out of scope here.  SRX chassis clusters are excluded:
    #    PyEZ facts on a cluster can report RE0 status "Absent" while that RE
    #    is master and up (issue #19, observed on an SRX4600 cluster), so
    #    RE0/RE1 facts are not trustworthy there; a genuinely failed node
    #    raises chassis alarms on the survivor, which check 2 catches.
    try:
        facts = dev.facts
        if facts.get("2RE") and not facts.get("srx_cluster"):
            for re_name in ("RE0", "RE1"):
                re_info = facts.get(re_name) or {}
                status = (re_info.get("status") or "").strip()
                if status.lower() in _RE_FAULT_STATES:
                    anomalies.append(f"[RE_FAULT] {re_name} status={status}")
    except Exception as exc:
        anomalies.append(f"[CHECK_ERROR] routing-engine: {exc}")

    # 6. Route-summary baseline (only when route_baseline is set). Flags an
    #    inet.0 destination count that deviates from the expected value — scope
    #    with tags (e.g. tags=["main"], route_baseline=152), since full-table
    #    routers carry far more routes than access routers.
    if route_baseline:
        try:
            out = dev.cli("show route summary", warning=False)
            m = _ROUTE_INET0_RE.search(out)
            if m:
                dest = int(m.group(1))
                if dest != route_baseline:
                    anomalies.append(
                        f"[ROUTE_BASELINE] inet.0 {dest} destinations (baseline {route_baseline})"
                    )
        except Exception as exc:
            anomalies.append(f"[CHECK_ERROR] route summary: {exc}")

    return {"hostname": hostname, "anomalies": anomalies, "error": None}


@mcp.tool()
def daily_brief(
    hostnames: list[str] | None = None,
    tags: list[str] | None = None,
    since_hours: int = 18,
    route_baseline: int = 0,
    max_workers: int = 10,
    config_path: str = "",
) -> str:
    """Run a morning health check across multiple devices in parallel.

    Checks per host (Phase 1):
    - ``show system alarms`` / ``show chassis alarms``
    - ``show interfaces descriptions`` — a physical interface is flagged
      ``[IF_DOWN]`` only when it has a description (``Admin=up``, ``Link=down``)
      and its ``Last flapped`` time is within ``since_hours``.  Undescribed
      unused ports and chronically-down ports are suppressed (loopback / mgmt /
      internal logical units are also excluded).
    - ``show log messages | last 200`` — alert patterns within ``since_hours``
    - dual-RE redundancy — an explicit routing-engine fault is flagged
      ``[RE_FAULT]`` (skipped on SRX chassis clusters, whose facts misreport
      RE status; a failed cluster node raises chassis alarms instead)
    - ``route_baseline`` (optional) — when > 0, a device whose ``inet.0``
      destination count differs from this value is flagged ``[ROUTE_BASELINE]``.
      Scope with ``tags`` (e.g. ``tags=["main"], route_baseline=152``), since
      full-table routers carry far more routes than access routers.

    Syslog patterns watched: BGP state change away from Established, STP port
    role change, OSPF neighbor down, ARP address conflict, IF_DOWN.

    ``since_hours`` defaults to 18 (≈ previous 15:00 for a 09:00 morning run).
    Tags default to none (all routers); pass ``tags=["main"]`` to limit scope.

    Output tiers:
    - CRITICAL — connection failure
    - WARNING  — at least one anomaly found
    - OK       — clean

    Returns a Markdown summary with anomaly details for CRITICAL/WARNING hosts
    and a collapsed OK list.
    """
    err = _ensure_config(config_path)
    if err:
        return err

    targets = _resolve_hostnames(hostnames, tags)
    if isinstance(targets, str):
        return targets

    norm_path = _resolve_config_path(config_path)

    # Pre-populate host option to avoid a ConfigParser race inside threads
    for hostname in targets:
        if not common.config.has_option(hostname, "host"):
            common.config.set(hostname, "host", hostname)

    pool = get_pool()

    def _run_one(hostname: str) -> dict:
        if pool is not None:
            try:
                with pool.acquire(hostname, norm_path) as dev:
                    return _check_host_health(hostname, dev, since_hours, route_baseline)
            except PoolConnectionError as exc:
                return {
                    "hostname": hostname,
                    "anomalies": [f"[UNREACHABLE] {exc}"],
                    "error": str(exc),
                }
        conn = common.connect(hostname)
        if not conn.get("ok"):
            msg = conn.get("error_message") or conn.get("error") or "Connection failed"
            return {
                "hostname": hostname,
                "anomalies": [f"[UNREACHABLE] {msg}"],
                "error": msg,
            }
        dev = conn["dev"]
        try:
            return _check_host_health(hostname, dev, since_hours, route_baseline)
        finally:
            try:
                dev.close()
            except Exception:
                pass

    results = common.run_parallel(_run_one, targets, max_workers=max_workers)

    now_str = (
        datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    )

    criticals, warnings, oks = [], [], []
    for hostname in targets:
        row = results.get(hostname)
        if not isinstance(row, dict):
            # common.run_parallel stores a sentinel (int 1) for a worker that
            # raised; coerce any non-dict so one bad host cannot crash the
            # whole render at row.get(...) below.
            row = {
                "hostname": hostname,
                "anomalies": ["[UNREACHABLE] no result"],
                "error": "no result",
            }
        if row.get("error"):
            criticals.append(row)
        elif row.get("anomalies"):
            warnings.append(row)
        else:
            oks.append(row)

    lines: list[str] = [
        f"## daily_brief — {now_str} (since -{since_hours}h)",
        f"## {len(targets)} hosts: "
        f"{len(oks)} OK, {len(warnings)} WARNING, {len(criticals)} CRITICAL",
        "",
    ]

    if criticals:
        lines.append("### CRITICAL")
        for row in criticals:
            lines.append(f"- **{row['hostname']}**: {row['error']}")
        lines.append("")

    if warnings:
        lines.append("### WARNINGS")
        for row in warnings:
            lines.append(f"#### {row['hostname']}")
            for anomaly in row["anomalies"]:
                lines.append(f"- {anomaly}")
        lines.append("")

    ok_names = ", ".join(r["hostname"] for r in oks)
    lines.append(f"### OK hosts ({len(oks)})")
    if ok_names:
        lines.append(ok_names)

    return "\n".join(lines)


@mcp.tool()
def health_check(config_path: str = "") -> dict:
    """Report server version and config status — without connecting to any device.

    Call this at session start (or after a tool-call timeout) to confirm the MCP
    is up, see which version is running, and verify that config.ini loads and how
    many routers it defines. Lightweight by design: junos-mcp fans out to many
    Juniper devices, so this check ONLY loads config.ini and counts hosts — it
    opens NO NETCONF/SSH connection to any device.

    Always returns the same keys: ``status`` (healthy / degraded / error),
    ``service``, ``version``, ``config_path`` (the resolved config.ini path it
    would use), ``router_count`` (number of host sections in config.ini),
    ``tags`` (sorted list of distinct tags across configured hosts), and
    ``config`` (ok / error / missing). On a degraded or error result, ``detail``
    carries the reason.

    Args:
        config_path: Path to config.ini (empty string uses default search).
    """
    from junos_mcp import __version__

    # Fixed shape: every key is present regardless of outcome, so callers can
    # read it uniformly and rely on `status` to judge health.
    result: dict = {
        "status": "healthy",
        "service": "junos-mcp",
        "version": __version__,
        "config_path": _resolve_config_path(config_path),
        "router_count": 0,
        "tags": [],
        "config": "ok",
    }

    # Loading the config is purely local (no device round trip). A genuine
    # parse/missing error degrades the server; anything unexpected is caught so
    # the health check never raises.
    try:
        err = _ensure_config(config_path)
        if err:
            # _init_globals returns a "Config error: ..." string on failure;
            # treat a missing file as "missing", any other parse failure as "error".
            cfg_path = result["config_path"]
            result["config"] = "missing" if not os.path.exists(cfg_path) else "error"
            result["status"] = "error"
            result["detail"] = err
            return result

        sections = common.config.sections()
        result["router_count"] = len(sections)
        tags: set[str] = set()
        for section in sections:
            tags |= common._get_host_tags(section)
        result["tags"] = sorted(tags)
    except Exception as e:  # noqa: BLE001 — surface config errors, don't sink the check
        result["status"] = "error"
        result["config"] = "error"
        result["detail"] = str(e)

    return result


if __name__ == "__main__":
    import argparse as _ap

    _parser = _ap.ArgumentParser()
    _parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    _args = _parser.parse_args()
    mcp.run(transport=_args.transport)
