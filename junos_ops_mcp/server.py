"""MCP server exposing junos-ops device operations.

Provides read-only tools for Juniper Networks device management:
get_device_facts, get_version, run_show_command, list_remote_files.

STDIO transport is used for JSON-RPC communication. All junos-ops
print() output is captured via contextlib.redirect_stdout to avoid
corrupting the transport.
"""

import argparse
import contextlib
import io
import os
import os.path
from pprint import pformat

from mcp.server.fastmcp import FastMCP

from junos_ops import common
from junos_ops import upgrade

mcp = FastMCP("junos-ops")


def _init_globals(config_path: str = "") -> str | None:
    """Initialize junos-ops global state (common.args and common.config).

    :param config_path: Path to config.ini. Empty string uses default search.
    :return: Error message string, or None on success.
    """
    # args の初期化（conftest.py と同パターン）
    common.args = argparse.Namespace(
        debug=False,
        dry_run=False,
        force=False,
        config=os.path.expanduser(config_path) if config_path else common.get_default_config(),
        list_format=None,
        copy=False,
        install=False,
        update=False,
        showversion=False,
        rollback=False,
        rebootat=None,
        configfile=None,
        confirm_timeout=1,
        health_check=None,
        show_command=None,
        showfile=None,
        specialhosts=[],
    )

    # config 読み込み（stdout に出力される可能性があるのでキャプチャ）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        err = common.read_config()
    if err:
        captured = buf.getvalue().strip()
        return f"Config error: {captured}" if captured else "Config file is empty or not found"
    return None


def _capture_stdout(func, *args, **kwargs):
    """Run func with stdout captured, return (result, captured_text).

    junos-ops の関数は print() で結果を出力するため、
    MCP の STDIO トランスポートを汚染しないようキャプチャする。
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

    # 接続（stdout キャプチャ付き）
    err_flag, dev = None, None
    connect_output = ""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        err_flag, dev = common.connect(hostname)
    connect_output = buf.getvalue().strip()

    if err_flag or dev is None:
        return f"Connection error: {connect_output}" if connect_output else "Connection failed"

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
        result, captured = _capture_stdout(upgrade.show_version, hostname, dev)
        output = f"# {hostname}\n{captured.strip()}"
        if result:
            output += "\n\nWARNING: show_version reported an error"
        return output

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
def list_remote_files(hostname: str, config_path: str = "") -> str:
    """List files on the remote device path (/var/tmp by default).

    Args:
        hostname: Target device hostname (must exist in config.ini)
        config_path: Path to config.ini (empty string uses default search)
    """
    def _operation(hostname, dev):
        # list_format を long に設定して詳細表示
        original_format = common.args.list_format
        common.args.list_format = None  # long format
        try:
            result, captured = _capture_stdout(upgrade.list_remote_path, hostname, dev)
            return f"# {hostname}\n{captured.strip()}"
        finally:
            common.args.list_format = original_format

    return _connect_and_run(hostname, config_path, _operation)


if __name__ == "__main__":
    mcp.run(transport="stdio")
