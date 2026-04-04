"""MCP server exposing junos-ops device operations.

Provides 19 tools for Juniper Networks device management: device info,
CLI execution, config management, firmware upgrade, and RSI/SCF collection.

Supports STDIO and Streamable HTTP transports.

junos-ops 0.14.0 migrated its core functions to dict returns
(connect, read_config, copy, install, rollback, reboot, load_config,
list_remote_path, rsi.collect_rsi, ...). MCP tools use those dicts
directly for error detection and status reporting. Human-readable
formatting still goes through the ``junos_ops.display`` layer, which
prints to stdout — we capture that text with ``contextlib.redirect_stdout``
so the MCP STDIO JSON-RPC channel is never corrupted, and so we also
sweep up the last few pre-v0.14.0 direct prints that remain in
``check_local_package`` / ``check_remote_package`` / ``clear_reboot``.
"""

import argparse
import contextlib
import io
import os
import os.path
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


def _run_with_display(core_func, display_func, *args, **kwargs):
    """Run a junos-ops core function and pipe its result through the display layer.

    junos-ops 0.14.0+ core functions return structured dicts and do not
    print. The ``display`` layer renders those dicts for humans by
    printing to stdout. This helper runs both under a stdout redirect
    so the MCP response string is built from the captured text — the
    redirect also catches the few remaining direct ``print()`` calls
    in ``check_local_package`` / ``check_remote_package`` / ``clear_reboot``
    that have not yet been migrated to dict returns.

    :return: ``(result_dict, captured_text)``.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = core_func(*args, **kwargs)
        display_func(result)
    return result, buf.getvalue()


def _capture_stdout(func, *args, **kwargs):
    """Legacy helper: run ``func`` with stdout captured.

    Kept for a handful of paths that do not yet have a display counterpart
    (e.g. ``get_pending_version``) or that only need stdout protection.
    Prefer :func:`_run_with_display` for dict-returning core functions.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


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
        result, captured = _run_with_display(
            upgrade.show_version, display.print_version, hostname, dev
        )
        return f"# {hostname}\n{captured.strip()}"

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


@mcp.tool()
def run_show_command_batch(
    hostnames: list[str],
    command: str,
    max_workers: int = 5,
    config_path: str = "",
) -> str:
    """Run a CLI show command on multiple devices in parallel.

    Uses ThreadPoolExecutor for concurrent execution.

    Args:
        hostnames: List of target device hostnames (must exist in config.ini)
        command: CLI command to execute on all devices
        max_workers: Maximum parallel threads (default 5)
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err

    def _run_one(hostname):
        return run_show_command(hostname, command, config_path)

    results = common.run_parallel(_run_one, hostnames, max_workers=max_workers)
    parts = [results[h] for h in hostnames if h in results]
    return "\n\n".join(parts)


@mcp.tool()
def list_remote_files(hostname: str, config_path: str = "") -> str:
    """List files on the remote device path (/var/tmp by default).

    Args:
        hostname: Target device hostname (must exist in config.ini)
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        # list_format controls short vs long rendering in display.print_list_remote.
        original_format = common.args.list_format
        common.args.list_format = "long"
        try:
            result, captured = _run_with_display(
                upgrade.list_remote_path, display.print_list_remote, hostname, dev
            )
            return f"# {hostname}\n{captured.strip()}"
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

        # dry_run() performs the local/remote package checks and returns
        # ``ok=True`` iff both sides are present+verified. Its internal
        # helpers still print, so we use _run_with_display to capture.
        result, captured = _run_with_display(
            upgrade.dry_run, display.print_dry_run, hostname, dev
        )
        status = "READY" if result["ok"] else "NOT READY"
        return f"# {hostname}\nUpgrade readiness: {status}\n{captured.strip()}"

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
def get_router_list(config_path: str = "") -> str:
    """List all available routers defined in config.ini.

    Returns section names from config.ini, which represent
    the hostnames that can be used with other tools.
    No device connection required.

    Args:
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err
    sections = common.config.sections()
    if not sections:
        return "No routers defined in config"
    return f"Available routers ({len(sections)}):\n" + "\n".join(
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
        # junos-ops 0.14.0. It writes the SCF and RSI files and returns
        # a structured result with paths and byte counts.
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
    hostnames: list[str],
    output_dir: str = "",
    max_workers: int = 20,
    config_path: str = "",
) -> str:
    """Collect RSI/SCF from multiple devices in parallel.

    Uses ThreadPoolExecutor for concurrent collection.
    Default 20 workers matches junos-ops CLI default for RSI collection.

    Args:
        hostnames: List of target device hostnames
        output_dir: Directory to save output files (empty uses config RSI_DIR or current dir)
        max_workers: Maximum parallel threads (default 20)
        config_path: Path to config.ini (empty string uses default search)
    """
    err = _ensure_config(config_path)
    if err:
        return err

    def _run_one(hostname):
        return collect_rsi(hostname, output_dir, config_path)

    results = common.run_parallel(_run_one, hostnames, max_workers=max_workers)
    parts = [results[h] for h in hostnames if h in results]
    return "\n\n".join(parts)


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
            Default: ["ping count 3 255.255.255.255 rapid"]
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
            health_cmds = health_check if health_check is not None else [
                "ping count 3 255.255.255.255 rapid"
            ]
            health_failed, health_output = _capture_stdout(
                upgrade._run_health_check, hostname, dev, health_cmds
            )

            if health_failed:
                # ヘルスチェック失敗 — confirm せずタイマー満了で自動ロールバック
                cu.unlock()
                return (
                    f"# {hostname}\n## Config diff\n{diff}\n"
                    f"## HEALTH CHECK FAILED\n{health_output.strip()}\n"
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
        result, captured = _run_with_display(
            upgrade.copy, display.print_copy, hostname, dev
        )
        status = "OK" if result.get("ok") else "FAILED"
        if result.get("skipped"):
            status = f"SKIPPED ({result.get('skip_reason')})"
        return f"# {hostname}\n## copy_package: {status}\n{captured.strip()}"

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
        result, captured = _run_with_display(
            upgrade.install, display.print_install, hostname, dev
        )
        status = "OK" if result.get("ok") else "FAILED"
        if result.get("skipped"):
            status = f"SKIPPED ({result.get('skip_reason')})"
        return f"# {hostname}\n## install_package: {status}\n{captured.strip()}"

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

        result, captured = _run_with_display(
            upgrade.rollback, display.print_rollback, hostname, dev
        )
        status = "OK" if result.get("ok") else "FAILED"
        return (
            f"# {hostname}\n"
            f"Pending version: {pending}\n"
            f"## rollback_package: {status}\n{captured.strip()}"
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
        result, captured = _run_with_display(
            upgrade.reboot, display.print_reboot, hostname, dev, reboot_dt
        )
        # reboot() preserves legacy exit codes in result["code"]
        # (0 success; 2..6 various failure modes).
        status = "OK" if result.get("ok") else f"FAILED (code={result.get('code')})"
        return f"# {hostname}\n## schedule_reboot: {status}\n{captured.strip()}"

    return _connect_and_run(hostname, config_path, _operation)


if __name__ == "__main__":
    import argparse as _ap

    _parser = _ap.ArgumentParser()
    _parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    _args = _parser.parse_args()
    mcp.run(transport=_args.transport)
