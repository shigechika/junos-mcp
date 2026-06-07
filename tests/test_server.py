"""Tests for junos-mcp server tools."""

import argparse
import configparser
import os
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

import junos_mcp.pool as pool_module
from junos_ops import common
from junos_mcp.server import (
    _check_host_health,
    _connect_and_run,
    _ensure_config,
    _init_globals,
    _resolve_config_path,
    _syslog_line_dt,
    check_local_inventory,
    check_reachability,
    check_remote_packages,
    check_upgrade_readiness,
    collect_rsi,
    collect_rsi_batch,
    compare_version,
    copy_package,
    daily_brief,
    install_package,
    push_config,
    rollback_package,
    schedule_reboot,
    get_config,
    get_config_diff,
    get_device_facts,
    get_package_info,
    get_router_list,
    get_version,
    list_remote_files,
    run_show_command,
    run_show_command_batch,
    run_show_commands,
)


@pytest.fixture(autouse=True)
def reset_globals():
    """各テスト前にグローバル変数をリセット"""
    original_args = common.args
    original_config = common.config
    yield
    common.args = original_args
    common.config = original_config


@pytest.fixture(autouse=True)
def reset_pool():
    """テスト間でプールキャッシュが干渉しないようシングルトンをリセット"""
    pool_module._pool = None
    yield
    pool_module._pool = None


@pytest.fixture
def mock_config():
    """テスト用の config を設定"""
    cfg = configparser.ConfigParser(allow_no_value=True)
    cfg.read_dict(
        {
            "DEFAULT": {
                "id": "testuser",
                "pw": "testpass",
                "sshkey": "id_ed25519",
                "port": "830",
                "hashalgo": "md5",
                "rpath": "/var/tmp",
            },
            "rt1.example.jp": {"host": "192.0.2.1"},
        }
    )
    common.config = cfg
    common.args = argparse.Namespace(
        debug=False,
        dry_run=False,
        force=False,
        config="config.ini",
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
    return cfg


# --- _init_globals ---


class TestInitGlobals:
    def test_init_with_valid_config(self, tmp_path):
        """有効な config.ini で初期化できる"""
        config_file = tmp_path / "config.ini"
        config_file.write_text(
            "[DEFAULT]\n"
            "id = testuser\n"
            "pw = testpass\n"
            "sshkey = id_ed25519\n"
            "port = 830\n"
            "hashalgo = md5\n"
            "rpath = /var/tmp\n"
            "\n"
            "[rt1.example.jp]\n"
        )
        err = _init_globals(str(config_file))
        assert err is None
        assert common.args is not None
        assert common.config is not None
        assert common.config.has_section("rt1.example.jp")

    def test_init_with_empty_config(self, tmp_path):
        """空の config.ini でエラーを返す"""
        config_file = tmp_path / "config.ini"
        config_file.write_text("")
        err = _init_globals(str(config_file))
        assert err is not None
        assert "Config" in err

    def test_init_sets_args_defaults(self, tmp_path):
        """args のデフォルト値が正しく設定される"""
        config_file = tmp_path / "config.ini"
        config_file.write_text("[rt1.example.jp]\n")
        _init_globals(str(config_file))
        assert common.args.debug is False
        assert common.args.dry_run is False
        assert common.args.force is False

    def test_init_expands_tilde(self, tmp_path, monkeypatch):
        """~ がホームディレクトリに展開される"""
        config_file = tmp_path / "config.ini"
        config_file.write_text("[rt1.example.jp]\n")
        # os.path.expanduser consults HOME on POSIX and USERPROFILE on
        # Windows (HOME is only consulted as a Windows fallback), so
        # patch both to keep the test cross-platform.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        _init_globals("~/config.ini")
        # Normalize separators: the literal "/" in "~/config.ini" is
        # preserved verbatim by expanduser on Windows, producing a
        # mixed-separator path that normpath canonicalizes.
        assert os.path.normpath(common.args.config) == str(config_file)

    def test_init_uses_env_var(self, tmp_path, monkeypatch):
        """JUNOS_OPS_CONFIG 環境変数からパスを取得"""
        config_file = tmp_path / "config.ini"
        config_file.write_text("[rt1.example.jp]\n")
        monkeypatch.setenv("JUNOS_OPS_CONFIG", str(config_file))
        _init_globals("")
        assert common.args.config == str(config_file)


# --- _resolve_config_path ---


class TestResolveConfigPath:
    def test_argument_takes_priority(self, tmp_path, monkeypatch):
        """引数が環境変数より優先される"""
        monkeypatch.setenv("JUNOS_OPS_CONFIG", "/other/config.ini")
        result = _resolve_config_path(str(tmp_path / "config.ini"))
        assert result == str(tmp_path / "config.ini")

    def test_env_var_when_no_argument(self, monkeypatch):
        """引数なしで環境変数が使われる"""
        monkeypatch.setenv("JUNOS_OPS_CONFIG", "/env/config.ini")
        result = _resolve_config_path("")
        assert result == "/env/config.ini"

    def test_env_var_tilde_expanded(self, tmp_path, monkeypatch):
        """環境変数の ~ も展開される"""
        # Patch both HOME (POSIX) and USERPROFILE (Windows) — see the
        # comment in test_init_expands_tilde for why this is needed.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("JUNOS_OPS_CONFIG", "~/.config/junos-ops/config.ini")
        result = _resolve_config_path("")
        assert os.path.normpath(result) == str(
            tmp_path / ".config" / "junos-ops" / "config.ini"
        )

    def test_default_when_no_argument_no_env(self, monkeypatch):
        """引数も環境変数もない場合はデフォルト探索"""
        monkeypatch.delenv("JUNOS_OPS_CONFIG", raising=False)
        result = _resolve_config_path("")
        # get_default_config() の結果と一致するはず
        assert result == common.get_default_config()


# --- _ensure_config ---


class TestEnsureConfig:
    def test_skip_when_already_initialized(self, mock_config):
        """既に初期化済みの場合はスキップ"""
        err = _ensure_config("")
        assert err is None

    def test_skip_when_same_config_path(self, mock_config):
        """同じ config パスの場合はスキップ"""
        err = _ensure_config("config.ini")
        assert err is None

    def test_reinitialize_with_different_path(self, tmp_path):
        """異なる config パスの場合は再初期化"""
        config_file = tmp_path / "other.ini"
        config_file.write_text("[rt2.example.jp]\n")
        # まず初期化
        common.config = configparser.ConfigParser()
        common.args = argparse.Namespace(config="config.ini")
        # 異なるパスで再初期化
        err = _ensure_config(str(config_file))
        assert err is None
        assert common.config.has_section("rt2.example.jp")


# --- _connect_and_run ---


class TestConnectAndRun:
    def test_hostname_not_in_config(self, mock_config):
        """config にないホスト名でエラー"""
        result = _connect_and_run("unknown-host", "", lambda h, d: "ok")
        assert "not found in config" in result

    @patch("junos_ops.common.connect")
    def test_connection_failure(self, mock_connect, mock_config):
        """接続失敗時のエラーメッセージ"""
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": False, "dev": None, "error": "ConnectError", "error_message": "mock connect error"}
        result = _connect_and_run("rt1.example.jp", "", lambda h, d: "ok")
        assert "Connection" in result

    @patch("junos_ops.common.connect")
    def test_successful_operation(self, mock_connect, mock_config):
        """正常な操作の実行（プール有効時はデバイスを閉じずに再利用）"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = _connect_and_run(
            "rt1.example.jp", "", lambda h, d: f"success: {h}"
        )
        assert result == "success: rt1.example.jp"
        mock_dev.close.assert_not_called()

    @patch("junos_ops.common.connect")
    def test_device_closed_on_exception(self, mock_connect, mock_config):
        """例外発生時もデバイス接続が閉じられる"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}

        def raise_error(h, d):
            raise RuntimeError("test error")

        with pytest.raises(RuntimeError):
            _connect_and_run("rt1.example.jp", "", raise_error)
        mock_dev.close.assert_called_once()


# --- get_device_facts ---


class TestGetDeviceFacts:
    @patch("junos_ops.common.connect")
    def test_returns_facts(self, mock_connect, mock_config):
        """デバイス facts を返す"""
        mock_dev = MagicMock()
        mock_dev.facts = {
            "hostname": "rt1",
            "model": "EX2300-24T",
            "version": "22.4R3-S6.5",
            "serialnumber": "TEST123",
        }
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = get_device_facts("rt1.example.jp")
        assert "rt1.example.jp" in result
        assert "EX2300-24T" in result
        assert "22.4R3-S6.5" in result


# --- get_version ---


class TestGetVersion:
    @patch("junos_ops.common.connect")
    @patch("junos_mcp.server.upgrade.show_version")
    def test_returns_version_output(self, mock_show, mock_connect, mock_config):
        """バージョン情報を返す (junos-ops 0.14.0+ dict return)"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_show.return_value = {
            "hostname": "rt1",
            "model": "EX2300-24T",
            "running": "22.4R3-S6.5",
            "planning": "22.4R3-S6.5",
            "pending": None,
            "running_vs_planning": 0,
            "running_vs_pending": None,
            "commit": None,
            "rescue_config_epoch": None,
            "config_changed_after_install": False,
            "local_package": True,
            "remote_package": True,
            "reboot_scheduled": None,
        }
        result = get_version("rt1.example.jp")
        assert "rt1.example.jp" in result
        assert "EX2300-24T" in result
        mock_show.assert_called_once()


# --- run_show_command ---


class TestRunShowCommand:
    @patch("junos_ops.common.connect")
    def test_returns_command_output(self, mock_connect, mock_config):
        """CLI コマンドの出力を返す"""
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "BGP is running\nPeer: 192.0.2.1"
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_command("rt1.example.jp", "show bgp summary")
        assert "rt1.example.jp" in result
        assert "show bgp summary" in result
        assert "BGP is running" in result
        mock_dev.cli.assert_called_once_with("show bgp summary", warning=False)

    @patch("junos_ops.common.connect")
    def test_handles_command_error(self, mock_connect, mock_config):
        """コマンド実行エラーのハンドリング"""
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = Exception("RPC error")
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_command("rt1.example.jp", "show invalid")
        assert "RPC error" in result

    @patch("junos_ops.common.connect")
    def test_output_format_json(self, mock_connect, mock_config):
        """output_format='json' で dev.cli が format='json' で呼ばれる"""
        mock_dev = MagicMock()
        mock_dev.cli.return_value = {"bgp-information": {"peer-count": 2}}
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_command("rt1.example.jp", "show bgp summary", output_format="json")
        assert "rt1.example.jp" in result
        assert "show bgp summary" in result
        mock_dev.cli.assert_called_once_with("show bgp summary", warning=False, format="json")


# --- list_remote_files ---


class TestListRemoteFiles:
    @patch("junos_ops.common.connect")
    @patch("junos_mcp.server.upgrade.list_remote_path")
    def test_returns_file_list(self, mock_ls, mock_connect, mock_config):
        """ファイル一覧を返す"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_ls.return_value = {
            "hostname": "rt1.example.jp",
            "path": "/var/tmp",
            "files": [],
            "file_count": 0,
            "format": "long",
        }
        result = list_remote_files("rt1.example.jp")
        assert "rt1.example.jp" in result
        assert "/var/tmp" in result
        mock_ls.assert_called_once()

    @patch("junos_ops.common.connect")
    @patch("junos_mcp.server.upgrade.list_remote_path")
    def test_restores_list_format(self, mock_ls, mock_connect, mock_config):
        """list_format が元の値に復元される"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_ls.return_value = {
            "hostname": "rt1.example.jp",
            "path": "/var/tmp",
            "files": [],
            "file_count": 0,
            "format": "long",
        }
        common.args.list_format = "short"
        list_remote_files("rt1.example.jp")
        assert common.args.list_format == "short"


# --- check_upgrade_readiness ---


class TestCheckUpgradeReadiness:
    def _running_match(self, match: bool):
        return {
            "hostname": "rt1.example.jp",
            "running": "22.4R3-S6.5",
            "expected_file": "junos-arm-32-22.4R3-S6.5.tgz",
            "match": match,
        }

    def _dry_run(self, ok: bool):
        return {
            "hostname": "rt1.example.jp",
            "model": "EX2300-24T",
            "local_file": "junos-arm-32-22.4R3-S6.5.tgz",
            "planning_hash": "abc",
            "algo": "md5",
            "local_package": ok,
            "remote_package": ok,
            "ok": ok,
        }

    @patch("junos_ops.common.connect")
    @patch("junos_mcp.server.upgrade.check_running_package")
    def test_already_running_target(self, mock_check, mock_connect, mock_config):
        """既にターゲットバージョンで稼働中の場合"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_check.return_value = self._running_match(True)
        result = check_upgrade_readiness("rt1.example.jp")
        assert "already running the target version" in result
        assert "rt1.example.jp" in result

    @patch("junos_ops.common.connect")
    @patch("junos_mcp.server.upgrade.dry_run")
    @patch("junos_mcp.server.upgrade.check_running_package")
    def test_ready_for_upgrade(self, mock_check, mock_dry, mock_connect, mock_config):
        """アップグレード準備完了の場合"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_check.return_value = self._running_match(False)
        mock_dry.return_value = self._dry_run(True)
        result = check_upgrade_readiness("rt1.example.jp")
        assert "READY" in result
        assert "NOT READY" not in result

    @patch("junos_ops.common.connect")
    @patch("junos_mcp.server.upgrade.dry_run")
    @patch("junos_mcp.server.upgrade.check_running_package")
    def test_not_ready_for_upgrade(self, mock_check, mock_dry, mock_connect, mock_config):
        """アップグレード準備未完了の場合"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_check.return_value = self._running_match(False)
        mock_dry.return_value = self._dry_run(False)
        result = check_upgrade_readiness("rt1.example.jp")
        assert "NOT READY" in result


# --- compare_version ---


class TestCompareVersion:
    @patch("junos_mcp.server.upgrade.compare_version")
    def test_left_greater(self, mock_cmp):
        """left > right の場合"""
        mock_cmp.return_value = 1
        result = compare_version("23.2R1", "22.4R3")
        assert "23.2R1 > 22.4R3" in result

    @patch("junos_mcp.server.upgrade.compare_version")
    def test_equal(self, mock_cmp):
        """left == right の場合"""
        mock_cmp.return_value = 0
        result = compare_version("22.4R3", "22.4R3")
        assert "22.4R3 == 22.4R3" in result

    @patch("junos_mcp.server.upgrade.compare_version")
    def test_left_less(self, mock_cmp):
        """left < right の場合"""
        mock_cmp.return_value = -1
        result = compare_version("22.4R3", "23.2R1")
        assert "22.4R3 < 23.2R1" in result

    @patch("junos_mcp.server.upgrade.compare_version")
    def test_none_result(self, mock_cmp):
        """無効なバージョン文字列の場合"""
        mock_cmp.return_value = None
        result = compare_version("invalid", "22.4R3")
        assert "Error" in result
        assert "None" in result


# --- get_package_info ---


class TestGetPackageInfo:
    def test_returns_package_info(self, mock_config):
        """パッケージ情報を正常に取得"""
        with patch("junos_mcp.server.upgrade.get_model_file") as mock_file, \
             patch("junos_mcp.server.upgrade.get_model_hash") as mock_hash:
            mock_file.return_value = "junos-ex2300-22.4R3-S6.5.tgz"
            mock_hash.return_value = "abc123def456"
            result = get_package_info("rt1.example.jp", "EX2300-24T")
            assert "rt1.example.jp" in result
            assert "EX2300-24T" in result
            assert "junos-ex2300-22.4R3-S6.5.tgz" in result
            assert "abc123def456" in result

    def test_hostname_not_found(self, mock_config):
        """config にないホスト名でエラー"""
        result = get_package_info("unknown-host", "EX2300-24T")
        assert "not found in config" in result

    def test_model_not_configured(self, mock_config):
        """モデルが未設定の場合のエラー"""
        with patch("junos_mcp.server.upgrade.get_model_file") as mock_file:
            mock_file.side_effect = Exception("model 'UNKNOWN' not found")
            result = get_package_info("rt1.example.jp", "UNKNOWN")
            assert "Error" in result


# --- run_show_commands ---


class TestGetRouterList:
    def test_returns_router_list(self, mock_config):
        """ルータ一覧を返す"""
        result = get_router_list()
        assert "rt1.example.jp" in result
        assert "Available routers" in result

    def test_multiple_routers(self, mock_config):
        """複数ルータが定義されている場合"""
        mock_config.read_dict({"rt2.example.jp": {"host": "192.0.2.2"}})
        result = get_router_list()
        assert "rt1.example.jp" in result
        assert "rt2.example.jp" in result
        assert "(2)" in result


# --- run_show_commands ---


class TestRunShowCommands:
    @patch("junos_ops.common.connect")
    def test_multiple_commands(self, mock_connect, mock_config):
        """複数コマンドを順次実行"""
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = ["output1", "output2"]
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_commands(
            "rt1.example.jp", ["show version", "show interfaces"]
        )
        assert "rt1.example.jp" in result
        assert "show version" in result
        assert "show interfaces" in result
        assert "output1" in result
        assert "output2" in result
        assert mock_dev.cli.call_count == 2

    @patch("junos_ops.common.connect")
    def test_partial_failure(self, mock_connect, mock_config):
        """一部コマンドが失敗しても他は実行される"""
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = ["ok", Exception("RPC error")]
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_commands(
            "rt1.example.jp", ["show version", "show bad"]
        )
        assert "ok" in result
        assert "RPC error" in result

    @patch("junos_ops.common.connect")
    def test_empty_commands(self, mock_connect, mock_config):
        """空のコマンドリスト"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_commands("rt1.example.jp", [])
        assert "rt1.example.jp" in result
        mock_dev.cli.assert_not_called()


# --- run_show_command_batch ---


class TestRunShowCommandBatch:
    @patch("junos_ops.common.connect")
    def test_multiple_hosts(self, mock_connect, mock_config):
        """複数ホストに並列でコマンド実行"""
        mock_config.read_dict({"rt2.example.jp": {"host": "192.0.2.2"}})
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "BGP running"
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_command_batch(
            "show bgp summary",
            hostnames=["rt1.example.jp", "rt2.example.jp"],
            max_workers=2,
        )
        assert "rt1.example.jp" in result
        assert "rt2.example.jp" in result
        assert "BGP running" in result

    @patch("junos_ops.common.connect")
    def test_single_host(self, mock_connect, mock_config):
        """1台でも動作する"""
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "ok"
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_command_batch(
            "show version", hostnames=["rt1.example.jp"], max_workers=1
        )
        assert "rt1.example.jp" in result
        assert "ok" in result

    def test_host_not_in_config(self, mock_config):
        """config にないホストが含まれる場合"""
        result = run_show_command_batch(
            ["unknown-host"], "show version"
        )
        assert "not found in config" in result

    @patch("junos_ops.common.connect")
    def test_grep_pattern_matches(self, mock_connect, mock_config):
        """grep_pattern にマッチする行だけが返る（ヘッダー行は保持）"""
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "inet.0: 100 destinations\ninet6.0: 50 destinations\n"
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_command_batch(
            "show route summary",
            hostnames=["rt1.example.jp"],
            grep_pattern=r"inet\.0:",
        )
        assert "rt1.example.jp" in result
        assert "## show route summary" in result
        assert "inet.0:" in result
        assert "inet6.0:" not in result

    @patch("junos_ops.common.connect")
    def test_grep_pattern_no_match(self, mock_connect, mock_config):
        """grep_pattern にマッチする行がない場合は (no match) が返る"""
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "inet.0: 100 destinations\n"
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = run_show_command_batch(
            "show route summary",
            hostnames=["rt1.example.jp"],
            grep_pattern=r"mpls\.0:",
        )
        assert "rt1.example.jp" in result
        assert "(no match)" in result

    def test_grep_pattern_invalid(self, mock_config):
        """不正な正規表現は即エラーを返す"""
        result = run_show_command_batch(
            "show version",
            hostnames=["rt1.example.jp"],
            grep_pattern=r"[invalid",
        )
        assert "Error: invalid grep_pattern" in result

    @patch("junos_ops.common.connect")
    def test_grep_pattern_connection_error_not_hidden(self, mock_connect, mock_config):
        """接続エラーは grep_pattern でフィルタされず、そのまま返る"""
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": False, "dev": None, "error": "ConnectError", "error_message": "connection refused"}
        result = run_show_command_batch(
            "show version",
            hostnames=["rt1.example.jp"],
            grep_pattern=r"inet\.0:",
        )
        assert "Connection error" in result
        assert "(no match)" not in result


# --- get_config ---


class TestGetConfig:
    @patch("junos_ops.common.connect")
    def test_returns_text_config(self, mock_connect, mock_config):
        """テキスト形式で config を取得"""
        mock_dev = MagicMock()
        mock_config_elem = MagicMock()
        mock_config_elem.text = "set system host-name rt1\n"
        mock_dev.rpc.get_config.return_value = mock_config_elem
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = get_config("rt1.example.jp")
        assert "rt1.example.jp" in result
        assert "host-name" in result

    @patch("junos_ops.common.connect")
    def test_rpc_error(self, mock_connect, mock_config):
        """RPC エラーのハンドリング"""
        mock_dev = MagicMock()
        mock_dev.rpc.get_config.side_effect = Exception("permission denied")
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        result = get_config("rt1.example.jp")
        assert "Error" in result
        assert "permission denied" in result


# --- get_config_diff ---


class TestGetConfigDiff:
    @patch("junos_ops.common.connect")
    def test_returns_diff(self, mock_connect, mock_config):
        """rollback との差分を表示"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.diff.return_value = "[edit system]\n-  host-name old;\n+  host-name new;"
            MockConfig.return_value = mock_cu
            result = get_config_diff("rt1.example.jp")
            assert "rt1.example.jp" in result
            assert "host-name" in result
            mock_cu.rollback.assert_any_call(1)
            mock_cu.rollback.assert_any_call(0)

    @patch("junos_ops.common.connect")
    def test_no_diff(self, mock_connect, mock_config):
        """差分なしの場合"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.diff.return_value = None
            MockConfig.return_value = mock_cu
            result = get_config_diff("rt1.example.jp")
            assert "No differences" in result


# --- collect_rsi ---


class TestCollectRsi:
    @patch("junos_mcp.server.rsi.collect_rsi")
    @patch("junos_ops.common.connect")
    def test_collects_scf_and_rsi(self, mock_connect, mock_rsi_collect, mock_config, tmp_path):
        """SCF と RSI を収集してファイル保存 (junos-ops 0.14.0+ dict core)"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}

        scf_path = str(tmp_path / "rt1.example.jp.SCF")
        rsi_path = str(tmp_path / "rt1.example.jp.RSI")
        mock_rsi_collect.return_value = {
            "hostname": "rt1.example.jp",
            "ok": True,
            "scf": {"path": scf_path, "bytes": 120, "command": "show configuration | display set"},
            "rsi": {"path": rsi_path, "bytes": 4096},
            "rsi_dir": str(tmp_path) + "/",
            "error": None,
            "error_message": None,
        }

        result = collect_rsi("rt1.example.jp", output_dir=str(tmp_path))
        assert "SCF saved" in result
        assert "RSI saved" in result
        assert scf_path in result
        assert rsi_path in result

    @patch("junos_mcp.server.rsi.collect_rsi")
    @patch("junos_ops.common.connect")
    def test_rsi_failure(self, mock_connect, mock_rsi_collect, mock_config, tmp_path):
        """RSI 取得失敗時のハンドリング (scf 成功, rsi_rpc 失敗)"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}

        scf_path = str(tmp_path / "rt1.example.jp.SCF")
        mock_rsi_collect.return_value = {
            "hostname": "rt1.example.jp",
            "ok": False,
            "scf": {"path": scf_path, "bytes": 50, "command": "show configuration | display set"},
            "rsi": None,
            "rsi_dir": str(tmp_path) + "/",
            "error": "rsi_rpc",
            "error_message": "RPC timeout",
        }

        result = collect_rsi("rt1.example.jp", output_dir=str(tmp_path))
        assert "SCF saved" in result
        assert "RSI failed" in result
        assert "RPC timeout" in result

    def test_hostname_not_found(self, mock_config):
        """config にないホスト名でエラー"""
        result = collect_rsi("unknown-host")
        assert "not found in config" in result


# --- collect_rsi_batch ---


class TestCollectRsiBatch:
    @patch("junos_mcp.server.rsi.collect_rsi")
    @patch("junos_ops.common.connect")
    def test_multiple_hosts(self, mock_connect, mock_rsi_collect, mock_config, tmp_path):
        """複数ホストで並列 RSI 収集"""
        mock_config.read_dict({"rt2.example.jp": {"host": "192.0.2.2"}})
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}

        def _make_result(hostname):
            return {
                "hostname": hostname,
                "ok": True,
                "scf": {"path": f"{tmp_path}/{hostname}.SCF", "bytes": 10, "command": "show configuration | display set"},
                "rsi": {"path": f"{tmp_path}/{hostname}.RSI", "bytes": 100},
                "rsi_dir": str(tmp_path) + "/",
                "error": None,
                "error_message": None,
            }
        mock_rsi_collect.side_effect = lambda h, d: _make_result(h)

        result = collect_rsi_batch(
            ["rt1.example.jp", "rt2.example.jp"],
            output_dir=str(tmp_path),
            max_workers=2,
        )
        assert "rt1.example.jp" in result
        assert "rt2.example.jp" in result


# --- check_reachability / check_local_inventory / check_remote_packages ---


class TestCheckReachability:
    @patch("junos_ops.common.connect")
    def test_ok_host(self, mock_connect, mock_config):
        """到達可能ホストは ok と返る"""
        mock_dev = MagicMock()
        mock_connect.return_value = {
            "hostname": "rt1.example.jp", "host": "rt1.example.jp",
            "ok": True, "dev": mock_dev, "error": None, "error_message": None,
        }
        result = check_reachability(["rt1.example.jp"], max_workers=1)
        assert "rt1.example.jp" in result
        assert "ok" in result
        # gather_facts=False, auto_probe=5 で呼ばれること
        _, kwargs = mock_connect.call_args
        assert kwargs.get("gather_facts") is False
        assert kwargs.get("auto_probe") == 5

    @patch("junos_ops.common.connect")
    def test_unreachable_host(self, mock_connect, mock_config):
        """到達不能ホストは fail で詳細メッセージ付き"""
        mock_connect.return_value = {
            "hostname": "rt1.example.jp", "host": "rt1.example.jp",
            "ok": False, "dev": None, "error": "ConnectError",
            "error_message": "TCP probe timeout",
        }
        result = check_reachability(["rt1.example.jp"], max_workers=1)
        assert "rt1.example.jp" in result
        assert "fail" in result
        assert "TCP probe timeout" in result

    @patch("junos_mcp.server.upgrade.get_disk_avail")
    @patch("junos_ops.common.connect")
    def test_disk_column_shown(self, mock_connect, mock_disk, mock_config):
        """接続成功時に avail ディスク列が表示される"""
        mock_dev = MagicMock()
        mock_connect.return_value = {
            "hostname": "rt1.example.jp", "host": "rt1.example.jp",
            "ok": True, "dev": mock_dev, "error": None, "error_message": None,
        }
        mock_disk.return_value = {"ok": True, "avail_mib": 2048, "filesystem": "/var/tmp", "error": None}
        result = check_reachability(["rt1.example.jp"], max_workers=1)
        assert "avail" in result
        mock_disk.assert_called_once()


class TestCheckLocalInventory:
    @patch("junos_mcp.server.upgrade.check_local_package_by_model")
    @patch("junos_mcp.server.upgrade.iter_configured_models")
    def test_lists_all_models(self, mock_iter, mock_check, mock_config):
        """全モデルを iterate して結果テーブルを返す"""
        mock_iter.return_value = ["mx204", "qfx5110"]
        mock_check.side_effect = lambda h, m: {
            "file": f"{m}.tgz", "local_file": f"/tmp/{m}.tgz",
            "status": "ok", "cached": False,
            "actual_hash": "abc", "expected_hash": "abc",
            "message": "checksum ok", "error": None,
        }
        result = check_local_inventory()
        assert "mx204" in result
        assert "qfx5110" in result

    @patch("junos_mcp.server.upgrade.check_local_package_by_model")
    @patch("junos_mcp.server.upgrade.iter_configured_models")
    def test_single_model_filter(self, mock_iter, mock_check, mock_config):
        """model 指定で iter は呼ばれない"""
        mock_check.return_value = {
            "file": "mx204.tgz", "local_file": "/tmp/mx204.tgz",
            "status": "ok", "cached": False,
            "actual_hash": "abc", "expected_hash": "abc",
            "message": "checksum ok", "error": None,
        }
        result = check_local_inventory(model="mx204")
        assert "mx204" in result
        mock_iter.assert_not_called()

    @patch("junos_mcp.server.upgrade.iter_configured_models")
    def test_empty_inventory(self, mock_iter, mock_config):
        """モデル定義が空ならその旨のメッセージ"""
        mock_iter.return_value = []
        result = check_local_inventory()
        assert "No models" in result


class TestCheckRemotePackages:
    @patch("junos_mcp.server.upgrade.check_remote_package_by_model")
    @patch("junos_ops.common.connect")
    def test_with_explicit_model(self, mock_connect, mock_remote, mock_config):
        """model 引数で device facts を引かずに remote check"""
        mock_dev = MagicMock()
        mock_connect.return_value = {
            "hostname": "rt1.example.jp", "host": "rt1.example.jp",
            "ok": True, "dev": mock_dev, "error": None, "error_message": None,
        }
        mock_remote.return_value = {
            "file": "mx204.tgz", "remote_path": "/var/tmp/mx204.tgz",
            "status": "ok", "cached": False, "message": "ok",
            "actual_hash": "abc", "expected_hash": "abc", "error": None,
        }
        result = check_remote_packages(["rt1.example.jp"], model="mx204", max_workers=1)
        assert "rt1.example.jp" in result
        assert "ok" in result
        # explicit model なので gather_facts=False で接続
        _, kwargs = mock_connect.call_args
        assert kwargs.get("gather_facts") is False
        mock_remote.assert_called_once_with("rt1.example.jp", mock_dev, "mx204")

    @patch("junos_ops.common.connect")
    def test_connect_failure(self, mock_connect, mock_config):
        """接続失敗時は remote check は実行されない"""
        mock_connect.return_value = {
            "hostname": "rt1.example.jp", "host": "rt1.example.jp",
            "ok": False, "dev": None, "error": "ConnectError",
            "error_message": "auth failed",
        }
        result = check_remote_packages(["rt1.example.jp"], model="mx204", max_workers=1)
        assert "fail" in result
        assert "auth failed" in result


# --- push_config ---


class TestPushConfig:
    def test_no_input(self, mock_config):
        """config_file も set_commands も指定なしでエラー"""
        result = push_config("rt1.example.jp")
        assert "specify config_file or set_commands" in result

    def test_both_inputs(self, mock_config):
        """config_file と set_commands 両方指定でエラー"""
        result = push_config(
            "rt1.example.jp",
            config_file="/tmp/test.set",
            set_commands=["set system host-name rt1"],
        )
        assert "not both" in result

    @patch("junos_ops.common.connect")
    def test_dry_run_default(self, mock_connect, mock_config):
        """dry_run（デフォルト）で diff 表示のみ"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.diff.return_value = "[edit system]\n+  host-name new;"
            MockConfig.return_value = mock_cu
            result = push_config(
                "rt1.example.jp",
                set_commands=["set system host-name new"],
            )
            assert "dry-run" in result
            assert "host-name" in result
            mock_cu.lock.assert_called_once()
            mock_cu.rollback.assert_called_once()
            mock_cu.unlock.assert_called_once()
            mock_cu.commit.assert_not_called()

    @patch("junos_ops.common.connect")
    def test_no_changes(self, mock_connect, mock_config):
        """変更なしの場合"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.diff.return_value = None
            MockConfig.return_value = mock_cu
            result = push_config(
                "rt1.example.jp",
                set_commands=["set system host-name rt1"],
            )
            assert "No changes" in result

    @patch("junos_mcp.server.upgrade._run_health_check")
    @patch("junos_ops.common.connect")
    def test_commit_confirmed_success(self, mock_connect, mock_health, mock_config):
        """commit confirmed + ヘルスチェック成功"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_health.return_value = {
            "ok": True,
            "passed_command": "ping ...",
            "commands": [],
            "steps": [],
            "message": "health check passed",
        }
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.diff.return_value = "[edit]\n+  host-name new;"
            MockConfig.return_value = mock_cu
            result = push_config(
                "rt1.example.jp",
                set_commands=["set system host-name new"],
                dry_run=False,
            )
            assert "permanent" in result
            mock_cu.commit_check.assert_called_once()
            # commit confirmed + 確定 commit の2回
            assert mock_cu.commit.call_count == 2

    @patch("junos_mcp.server.upgrade._run_health_check")
    @patch("junos_ops.common.connect")
    def test_health_check_failure(self, mock_connect, mock_health, mock_config):
        """ヘルスチェック失敗で自動ロールバック待ち"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_health.return_value = {
            "ok": False,
            "passed_command": None,
            "commands": [],
            "steps": [{"action": "health_check_error", "message": "\tno packets received"}],
            "message": "\thealth check: ping ...\n\tno packets received",
        }
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.diff.return_value = "[edit]\n+  bad-config;"
            MockConfig.return_value = mock_cu
            result = push_config(
                "rt1.example.jp",
                set_commands=["set system bad-config"],
                dry_run=False,
            )
            assert "HEALTH CHECK FAILED" in result
            assert "auto-rollback" in result
            # commit confirmed の1回のみ（確定 commit はしない）
            assert mock_cu.commit.call_count == 1

    @patch("junos_ops.common.connect")
    def test_config_file_set(self, mock_connect, mock_config, tmp_path):
        """.set ファイルからの config push (dry_run)"""
        set_file = tmp_path / "test.set"
        set_file.write_text("set system host-name rt1\nset system domain-name example.jp\n")
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.diff.return_value = "[edit system]\n+  domain-name example.jp;"
            MockConfig.return_value = mock_cu
            result = push_config(
                "rt1.example.jp",
                config_file=str(set_file),
            )
            assert "dry-run" in result
            assert "domain-name" in result

    @patch("junos_ops.common.connect")
    def test_lock_failure(self, mock_connect, mock_config):
        """config ロック取得失敗"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.lock.side_effect = Exception("locked by another user")
            MockConfig.return_value = mock_cu
            result = push_config(
                "rt1.example.jp",
                set_commands=["set system host-name new"],
            )
            assert "lock failed" in result

    @patch("junos_ops.common.connect")
    def test_no_commit_skips_final_commit(self, mock_connect, mock_config):
        """no_commit=True: commit confirmed のみでヘルスチェック・確定 commit なし"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        with patch("junos_mcp.server.Config") as MockConfig:
            mock_cu = MagicMock()
            mock_cu.diff.return_value = "[edit system]\n+  host-name new;"
            MockConfig.return_value = mock_cu
            result = push_config(
                "rt1.example.jp",
                set_commands=["set system host-name new"],
                dry_run=False,
                no_commit=True,
            )
            assert "auto-rollback" in result
            assert "no_commit=True" in result
            # commit confirmed の1回のみ（確定 commit はしない）
            assert mock_cu.commit.call_count == 1
            mock_cu.commit.assert_called_once_with(confirm=1)


# --- copy_package ---


def _copy_result(ok: bool, skipped: bool = False):
    return {
        "hostname": "rt1.example.jp",
        "ok": ok,
        "skipped": skipped,
        "skip_reason": "already_running" if skipped else None,
        "dry_run": True,
        "local_file": None,
        "remote_path": "/var/tmp",
        "checksum_algo": "md5",
        "storage_cleanup": None,
        "snapshot_delete": None,
        "steps": [],
        "error": None if ok else "scp_failed",
    }


def _install_result(ok: bool, skipped: bool = False):
    return {
        "hostname": "rt1.example.jp",
        "ok": ok,
        "skipped": skipped,
        "skip_reason": "already_running" if skipped else None,
        "dry_run": True,
        "pending": None,
        "planning": None,
        "compare": None,
        "rollback_result": None,
        "copy_result": None,
        "rescue_save": None,
        "install_message": None,
        "steps": [],
        "error": None if ok else "sw_install_failed",
    }


def _rollback_result(ok: bool):
    return {
        "hostname": "rt1.example.jp",
        "ok": ok,
        "dry_run": False,
        "rpc_output": None,
        "message": "",
        "error": None if ok else "unrecognized_response",
    }


def _reboot_result(code: int):
    return {
        "hostname": "rt1.example.jp",
        "ok": code == 0,
        "code": code,
        "dry_run": True,
        "reboot_at": "2601020304",
        "existing_schedule": None,
        "cleared_existing": False,
        "reinstall_result": None,
        "message": None,
        "steps": [],
        "error": None if code == 0 else "reboot_failed",
    }


class TestCopyPackage:
    @patch("junos_mcp.server.upgrade.copy")
    @patch("junos_ops.common.connect")
    def test_dry_run(self, mock_connect, mock_copy, mock_config):
        """dry_run でコピー内容を表示"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_copy.return_value = _copy_result(ok=True)
        result = copy_package("rt1.example.jp")
        assert "rt1.example.jp" in result
        assert "OK" in result
        mock_copy.assert_called_once()

    @patch("junos_mcp.server.upgrade.copy")
    @patch("junos_ops.common.connect")
    def test_copy_failure(self, mock_connect, mock_copy, mock_config):
        """コピー失敗"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_copy.return_value = _copy_result(ok=False)
        result = copy_package("rt1.example.jp", dry_run=False)
        assert "FAILED" in result


# --- install_package ---


class TestInstallPackage:
    @patch("junos_mcp.server.upgrade.install")
    @patch("junos_ops.common.connect")
    def test_dry_run(self, mock_connect, mock_install, mock_config):
        """dry_run でインストール内容を表示"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_install.return_value = _install_result(ok=True)
        result = install_package("rt1.example.jp")
        assert "rt1.example.jp" in result
        assert "OK" in result

    @patch("junos_mcp.server.upgrade.install")
    @patch("junos_ops.common.connect")
    def test_install_failure(self, mock_connect, mock_install, mock_config):
        """インストール失敗"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_install.return_value = _install_result(ok=False)
        result = install_package("rt1.example.jp", dry_run=False)
        assert "FAILED" in result

    @patch("junos_mcp.server.upgrade.install")
    @patch("junos_ops.common.connect")
    def test_sets_subcommand_upgrade(self, mock_connect, mock_install, mock_config):
        """install_package sets args.subcommand='upgrade' so install() picks
        the right remote_check branch (regression guard for the bug fixed
        in junos-ops f1beaaf)."""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_install.return_value = _install_result(ok=True)
        install_package("rt1.example.jp")
        # After the tool runs, common.args.subcommand should have been set.
        assert common.args.subcommand == "upgrade"

    @patch("junos_mcp.server.upgrade.install")
    @patch("junos_ops.common.connect")
    def test_unlink_flag(self, mock_connect, mock_install, mock_config):
        """unlink=True で common.args.unlink が True にセットされる"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_install.return_value = _install_result(ok=True)
        install_package("rt1.example.jp", unlink=True)
        assert common.args.unlink is True

    @patch("junos_mcp.server.upgrade.install")
    @patch("junos_ops.common.connect")
    def test_unlink_default_false(self, mock_connect, mock_install, mock_config):
        """unlink のデフォルトは False"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_install.return_value = _install_result(ok=True)
        install_package("rt1.example.jp")
        assert common.args.unlink is False


# --- rollback_package ---


class TestRollbackPackage:
    @patch("junos_mcp.server.upgrade.get_pending_version")
    @patch("junos_ops.common.connect")
    def test_no_pending(self, mock_connect, mock_pending, mock_config):
        """pending version なしでスキップ"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_pending.return_value = None
        result = rollback_package("rt1.example.jp")
        assert "skipped" in result

    @patch("junos_mcp.server.upgrade.rollback")
    @patch("junos_mcp.server.upgrade.get_pending_version")
    @patch("junos_ops.common.connect")
    def test_rollback_success(self, mock_connect, mock_pending, mock_rollback, mock_config):
        """ロールバック成功"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_pending.return_value = "22.4R3-S6.5"
        mock_rollback.return_value = _rollback_result(ok=True)
        result = rollback_package("rt1.example.jp")
        assert "22.4R3-S6.5" in result
        assert "OK" in result

    @patch("junos_mcp.server.upgrade.rollback")
    @patch("junos_mcp.server.upgrade.get_pending_version")
    @patch("junos_ops.common.connect")
    def test_rollback_failure(self, mock_connect, mock_pending, mock_rollback, mock_config):
        """ロールバック失敗"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_pending.return_value = "22.4R3-S6.5"
        mock_rollback.return_value = _rollback_result(ok=False)
        result = rollback_package("rt1.example.jp", dry_run=False)
        assert "FAILED" in result


# --- schedule_reboot ---


class TestScheduleReboot:
    def test_invalid_datetime(self, mock_config):
        """不正な日時フォーマットでエラー"""
        result = schedule_reboot("rt1.example.jp", "invalid")
        assert "Error" in result

    @patch("junos_mcp.server.upgrade.reboot")
    @patch("junos_ops.common.connect")
    def test_dry_run(self, mock_connect, mock_reboot, mock_config):
        """dry_run でリブートスケジュールを表示"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_reboot.return_value = _reboot_result(code=0)
        result = schedule_reboot("rt1.example.jp", "2601020304")
        assert "rt1.example.jp" in result
        assert "OK" in result

    @patch("junos_mcp.server.upgrade.reboot")
    @patch("junos_ops.common.connect")
    def test_reboot_failure(self, mock_connect, mock_reboot, mock_config):
        """リブートスケジュール失敗 (exit code 4 = ConnectError on reboot RPC)"""
        mock_dev = MagicMock()
        mock_connect.return_value = {"hostname": "rt1.example.jp", "host": "rt1.example.jp", "ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_reboot.return_value = _reboot_result(code=4)
        result = schedule_reboot("rt1.example.jp", "2601020304", dry_run=False)
        assert "FAILED" in result
        assert "code=4" in result


# --- _syslog_line_dt ---


class TestSyslogLineDt:
    def test_valid_line(self):
        """正常なタイムスタンプをパースできる"""
        line = "Jan 15 10:30:00 rt1 %DAEMON-3: something happened"
        result = _syslog_line_dt(line)
        assert result is not None
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 10
        assert result.minute == 30
        assert result.second == 0

    def test_subsecond_stripped(self):
        """小数秒付きタイムスタンプも正しくパースできる"""
        line = "Mar  5 08:00:00.123 rt1 %DAEMON: msg"
        result = _syslog_line_dt(line)
        assert result is not None
        assert result.month == 3
        assert result.second == 0

    def test_year_rollover(self):
        """未来の日付は前年として扱う（年末ロールオーバー）"""
        import datetime as _dt
        from unittest.mock import patch
        # now を 2026-01-01 00:30:00 に固定
        # "Dec 31" は最初 2026-12-31 23:59:59 として構築される（now.year=2026）
        # → now+1h(2026-01-01 01:30:00) より大幅に未来 → 前年 2025 に修正される
        fake_now = _dt.datetime(2026, 1, 1, 0, 30, 0)
        with patch("junos_mcp.server.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = fake_now
            mock_dt.datetime.strptime.side_effect = _dt.datetime.strptime
            mock_dt.datetime.side_effect = _dt.datetime
            mock_dt.timedelta.side_effect = _dt.timedelta
            line = "Dec 31 23:59:59 rt1 %DAEMON: msg"
            result = _syslog_line_dt(line)
        assert result is not None
        assert result.year == 2025
        assert result.month == 12
        assert result.day == 31

    def test_invalid_line(self):
        """パースできない行は None を返す"""
        assert _syslog_line_dt("") is None
        assert _syslog_line_dt("not a syslog line") is None
        assert _syslog_line_dt("Jan XX 10:00:00 host msg") is None


# --- _check_host_health ---


class TestCheckHostHealth:
    def _make_dev(self, system_alarms="No alarms currently active",
                  chassis_alarms="No alarms currently active",
                  descriptions="",
                  flapped=None,
                  syslog="",
                  facts=None,
                  route_summary=""):
        dev = MagicMock()
        dev.facts = facts if facts is not None else {"2RE": False}
        flaps = flapped or {}

        def _cli(cmd, **kw):
            if cmd == "show system alarms":
                return system_alarms
            if cmd == "show chassis alarms":
                return chassis_alarms
            if cmd == "show interfaces descriptions":
                return descriptions
            if cmd == "show log messages | last 200":
                return syslog
            if cmd == "show route summary":
                return route_summary
            if cmd.startswith("show interfaces "):
                iface = cmd.split()[2]
                ts = flaps.get(iface)
                head = f"Physical interface: {iface}, Enabled, Physical link is Down\n"
                if ts is None:
                    return head
                return head + f"  Last flapped   : {ts} JST (1w0d 00:00 ago)\n"
            raise KeyError(cmd)

        dev.cli.side_effect = _cli
        return dev

    @staticmethod
    def _recent_ts():
        import datetime as _dt
        return (_dt.datetime.now() - _dt.timedelta(hours=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def test_all_clean(self):
        """正常なデバイスは anomalies が空"""
        dev = self._make_dev()
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert result["hostname"] == "rt1.example.jp"
        assert result["anomalies"] == []
        assert result["error"] is None

    def test_system_alarm(self):
        """system alarms に内容があれば [ALARM] タグで anomaly を追加する"""
        dev = self._make_dev(system_alarms="1 alarms currently active\nAlarm time   Class  Description\nMay 25 09:00:00  Minor  FPC 0 DRAM Error")
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert any("[ALARM]" in a for a in result["anomalies"])

    def test_described_recent_down_reported(self):
        """description 付き・admin up・link down で直近 flap のポートを検出する"""
        desc = (
            "Interface       Admin Link Description\n"
            "ge-0/0/1        up    down To_uplink\n"
        )
        dev = self._make_dev(
            descriptions=desc, flapped={"ge-0/0/1": self._recent_ts()}
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert any("[IF_DOWN] ge-0/0/1" in a for a in result["anomalies"])

    def test_undescribed_port_not_reported(self):
        """description が無いポートは descriptions に現れず [IF_DOWN] にならない"""
        desc = (
            "Interface       Admin Link Description\n"
            "ge-0/0/2        up    up   To_core\n"
        )
        dev = self._make_dev(descriptions=desc)
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert not any("[IF_DOWN]" in a for a in result["anomalies"])

    def test_chronic_down_suppressed(self):
        """description 付きでも flap が since_hours より古ければ抑止する"""
        desc = (
            "Interface       Admin Link Description\n"
            "xe-0/1/1        up    down To_old\n"
        )
        dev = self._make_dev(
            descriptions=desc, flapped={"xe-0/1/1": "2025-06-20 05:10:25"}
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert not any("[IF_DOWN]" in a for a in result["anomalies"])

    def test_admin_down_excluded(self):
        """admin down（意図的な無効化）は report しない"""
        desc = (
            "Interface       Admin Link Description\n"
            "ge-0/0/3        down  down To_disabled\n"
        )
        dev = self._make_dev(
            descriptions=desc, flapped={"ge-0/0/3": self._recent_ts()}
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert not any("[IF_DOWN]" in a for a in result["anomalies"])

    def test_mgmt_and_internal_units_excluded(self):
        """管理系(me)と内部論理ユニット(.16386)は description 付き down でも除外する"""
        desc = (
            "Interface       Admin Link Description\n"
            "me0             up    down mgmt\n"
            "ge-0/0/1.16386  up    down internal\n"
            "ge-0/0/4        up    down To_real\n"
        )
        dev = self._make_dev(
            descriptions=desc,
            flapped={
                "me0": self._recent_ts(),
                "ge-0/0/1.16386": self._recent_ts(),
                "ge-0/0/4": self._recent_ts(),
            },
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        downs = [a for a in result["anomalies"] if "[IF_DOWN]" in a]
        assert len(downs) == 1 and "ge-0/0/4" in downs[0]

    def test_flap_unknown_reported_conservatively(self):
        """flap 時刻不明でも link down なら保守的に report する"""
        desc = (
            "Interface       Admin Link Description\n"
            "ge-0/0/5        up    down To_x\n"
        )
        dev = self._make_dev(descriptions=desc, flapped={})
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert any("[IF_DOWN] ge-0/0/5" in a for a in result["anomalies"])

    def test_syslog_in_window(self):
        """since_hours 以内の alert パターンを [SYSLOG] として検出する"""
        import datetime as _dt
        now = _dt.datetime.now()
        ts = now.strftime("%b %d %H:%M:%S").replace("  ", " ")
        line = f"{ts} rt1 RPD_BGP_NEIGHBOR_STATE_CHANGED: EBGP peer 10.0.0.1 (AS 65001) Established->Active"
        dev = self._make_dev(syslog=line)
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert any("[SYSLOG]" in a for a in result["anomalies"])

    def test_syslog_outside_window(self):
        """since_hours より古いログは無視する"""
        line = "Jan  1 00:00:00 rt1 RPD_BGP_NEIGHBOR_STATE_CHANGED: Established->Active"
        dev = self._make_dev(syslog=line)
        result = _check_host_health("rt1.example.jp", dev, since_hours=1)
        assert not any("[SYSLOG]" in a for a in result["anomalies"])

    def test_syslog_truncated_at_max(self):
        """SYSLOG マッチが _SYSLOG_MAX_MATCHES を超えたら truncated メッセージを追加する"""
        import datetime as _dt
        now = _dt.datetime.now()
        ts = now.strftime("%b %d %H:%M:%S").replace("  ", " ")
        lines = "\n".join(
            f"{ts} rt1 IF_DOWN: interface ge-0/0/{i} down"
            for i in range(12)  # 12 行 > _SYSLOG_MAX_MATCHES(10)
        )
        dev = self._make_dev(syslog=lines)
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        syslog_entries = [a for a in result["anomalies"] if "[SYSLOG]" in a]
        # 10件 + truncated メッセージ = 11エントリ
        assert len(syslog_entries) == 11
        assert any("truncated" in a for a in syslog_entries)

    # --- routing-engine redundancy ([RE_FAULT]) ---

    def test_re_fault_flagged(self):
        """An explicit RE fault on a dual-RE chassis is flagged [RE_FAULT]."""
        dev = self._make_dev(
            facts={"2RE": True, "RE0": {"status": "OK"}, "RE1": {"status": "Fault"}}
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert any("[RE_FAULT] RE1 status=Fault" in a for a in result["anomalies"])

    def test_re_healthy_backup_not_flagged(self):
        """A healthy backup RE (status Present/Backup, not 'OK') must NOT flag."""
        dev = self._make_dev(
            facts={"2RE": True, "RE0": {"status": "OK"}, "RE1": {"status": "Present"}}
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert not any("RE_FAULT" in a for a in result["anomalies"])

    def test_single_re_no_redundancy_check(self):
        dev = self._make_dev(facts={"2RE": False})
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert not any("RE_FAULT" in a for a in result["anomalies"])

    # --- route-summary baseline ([ROUTE_BASELINE]) ---

    def test_route_baseline_deviation_flagged(self):
        dev = self._make_dev(
            route_summary="inet.0: 160 destinations, 200 routes (160 active, 0 holddown, 0 hidden)"
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18, route_baseline=152)
        assert any("[ROUTE_BASELINE]" in a and "160" in a for a in result["anomalies"])

    def test_route_baseline_match_not_flagged(self):
        dev = self._make_dev(
            route_summary="inet.0: 152 destinations, 200 routes (152 active, 0 holddown, 0 hidden)"
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18, route_baseline=152)
        assert not any("ROUTE_BASELINE" in a for a in result["anomalies"])

    def test_route_baseline_zero_skips_check(self):
        """route_baseline=0 (default) does not run the route-summary check."""
        dev = self._make_dev()
        result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        assert not any("ROUTE_BASELINE" in a for a in result["anomalies"])

    def test_route_baseline_ignores_vrf_tables(self):
        """Routing-instance tables (VRF/mgmt) must not fire the inet.0 baseline."""
        dev = self._make_dev(
            route_summary=(
                "inet.0: 152 destinations, 200 routes (152 active, 0 holddown, 0 hidden)\n"
                "VRF-CUST.inet.0: 9999 destinations, 12000 routes (9999 active, 0 holddown, 0 hidden)"
            )
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18, route_baseline=152)
        assert not any("ROUTE_BASELINE" in a for a in result["anomalies"])

    def test_re_fault_all_states_flagged(self):
        """Every fault state (and a mixed-case value) is matched case-insensitively."""
        for state in ["Fault", "Fail", "Failed", "Offline", "Absent", "Empty", "Testing", "OFFLINE"]:
            dev = self._make_dev(
                facts={"2RE": True, "RE0": {"status": "OK"}, "RE1": {"status": state}}
            )
            result = _check_host_health("rt1.example.jp", dev, since_hours=18)
            assert any(f"[RE_FAULT] RE1 status={state}" in a for a in result["anomalies"]), state

    def test_re_check_error_isolated(self):
        """A dev.facts read failure degrades to [CHECK_ERROR]; other checks still run."""
        dev = self._make_dev()  # all CLI checks clean
        type(dev).facts = PropertyMock(side_effect=RuntimeError("rpc fail"))
        try:
            result = _check_host_health("rt1.example.jp", dev, since_hours=18)
        finally:
            del type(dev).facts  # avoid leaking the property onto the MagicMock class
        assert any("[CHECK_ERROR] routing-engine" in a for a in result["anomalies"])
        assert not any("[ALARM]" in a or "[IF_DOWN]" in a for a in result["anomalies"])

    def test_route_summary_check_error_isolated(self):
        """A show route summary failure degrades to [CHECK_ERROR], not a crash."""
        dev = self._make_dev()
        canned = dev.cli.side_effect

        def _cli(cmd, **kw):
            if cmd == "show route summary":
                raise RuntimeError("RpcTimeoutError")
            return canned(cmd, **kw)

        dev.cli.side_effect = _cli
        result = _check_host_health("rt1.example.jp", dev, since_hours=18, route_baseline=152)
        assert any("[CHECK_ERROR] route summary" in a for a in result["anomalies"])

    def test_route_baseline_no_inet0_line_no_op(self):
        """route_baseline with no inet.0 line in the output is a clean no-op."""
        dev = self._make_dev(
            route_summary="inet6.0: 30 destinations, 40 routes (30 active, 0 holddown, 0 hidden)"
        )
        result = _check_host_health("rt1.example.jp", dev, since_hours=18, route_baseline=152)
        assert not any("ROUTE_BASELINE" in a for a in result["anomalies"])
        assert not any("CHECK_ERROR" in a for a in result["anomalies"])


# --- daily_brief ---


class TestDailyBrief:
    @patch("junos_mcp.server.common.run_parallel")
    def test_all_ok(self, mock_parallel, mock_config):
        """全ホスト正常なら OK カウントが正しい"""
        mock_parallel.return_value = {
            "rt1.example.jp": {"hostname": "rt1.example.jp", "anomalies": [], "error": None}
        }
        result = daily_brief(hostnames=["rt1.example.jp"])
        assert "1 OK" in result
        assert "0 WARNING" in result
        assert "0 CRITICAL" in result

    @patch("junos_mcp.server.common.run_parallel")
    def test_warning_host(self, mock_parallel, mock_config):
        """anomaly があるホストは WARNING に分類される"""
        mock_parallel.return_value = {
            "rt1.example.jp": {
                "hostname": "rt1.example.jp",
                "anomalies": ["[IF_DOWN] ge-0/0/0"],
                "error": None,
            }
        }
        result = daily_brief(hostnames=["rt1.example.jp"])
        assert "1 WARNING" in result
        assert "ge-0/0/0" in result

    @patch("junos_mcp.server.common.run_parallel")
    def test_critical_host(self, mock_parallel, mock_config):
        """接続失敗は CRITICAL に分類される"""
        mock_parallel.return_value = {
            "rt1.example.jp": {
                "hostname": "rt1.example.jp",
                "anomalies": ["[UNREACHABLE] timeout"],
                "error": "timeout",
            }
        }
        result = daily_brief(hostnames=["rt1.example.jp"])
        assert "1 CRITICAL" in result
        assert "timeout" in result

    @patch("junos_mcp.server.common.run_parallel")
    def test_missing_result(self, mock_parallel, mock_config):
        """run_parallel が結果を返さなかったホストは CRITICAL"""
        mock_parallel.return_value = {}
        result = daily_brief(hostnames=["rt1.example.jp"])
        assert "1 CRITICAL" in result

    @patch("junos_mcp.server.common.run_parallel")
    def test_dropped_worker_sentinel_is_critical(self, mock_parallel, mock_config):
        """A non-dict sentinel (the int 1 run_parallel stores for a crashed
        worker) degrades to CRITICAL instead of crashing the whole brief."""
        mock_parallel.return_value = {"rt1.example.jp": 1}
        result = daily_brief(hostnames=["rt1.example.jp"])
        assert "1 CRITICAL" in result
        assert "no result" in result

    @patch("junos_mcp.server._check_host_health")
    @patch("junos_mcp.server.common.connect")
    @patch("junos_mcp.server.get_pool")
    def test_run_one_forwards_args_non_pool(self, mock_pool, mock_connect, mock_check, mock_config):
        """The non-pool path forwards since_hours and route_baseline positionally."""
        mock_pool.return_value = None
        mock_dev = MagicMock()
        mock_connect.return_value = {"ok": True, "dev": mock_dev, "error": None, "error_message": None}
        mock_check.return_value = {"hostname": "rt1.example.jp", "anomalies": [], "error": None}
        daily_brief(hostnames=["rt1.example.jp"], since_hours=24, route_baseline=152)
        mock_check.assert_called_once_with("rt1.example.jp", mock_dev, 24, 152)

    @patch("junos_mcp.server._check_host_health")
    @patch("junos_mcp.server.get_pool")
    def test_run_one_forwards_args_pool(self, mock_pool, mock_check, mock_config):
        """The pool path forwards since_hours and route_baseline positionally."""
        mock_dev = MagicMock()
        cm = MagicMock()
        cm.__enter__.return_value = mock_dev
        cm.__exit__.return_value = False
        pool = MagicMock()
        pool.acquire.return_value = cm
        mock_pool.return_value = pool
        mock_check.return_value = {"hostname": "rt1.example.jp", "anomalies": [], "error": None}
        daily_brief(hostnames=["rt1.example.jp"], since_hours=24, route_baseline=152)
        mock_check.assert_called_once_with("rt1.example.jp", mock_dev, 24, 152)

    def test_unknown_hostname(self, mock_config):
        """config.ini に存在しないホスト名はエラー"""
        result = daily_brief(hostnames=["unknown.example.jp"])
        assert "Error" in result
