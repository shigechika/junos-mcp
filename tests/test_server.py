"""Tests for junos-ops MCP server tools."""

import argparse
import configparser
from unittest.mock import MagicMock, patch

import pytest

from junos_ops import common
from junos_ops_mcp.server import (
    _capture_stdout,
    _connect_and_run,
    _ensure_config,
    _init_globals,
    _resolve_config_path,
    get_device_facts,
    get_version,
    list_remote_files,
    run_show_command,
)


@pytest.fixture(autouse=True)
def reset_globals():
    """各テスト前にグローバル変数をリセット"""
    original_args = common.args
    original_config = common.config
    yield
    common.args = original_args
    common.config = original_config


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
        monkeypatch.setenv("HOME", str(tmp_path))
        _init_globals("~/config.ini")
        assert common.args.config == str(config_file)

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
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("JUNOS_OPS_CONFIG", "~/.config/junos-ops/config.ini")
        result = _resolve_config_path("")
        assert result == str(tmp_path / ".config/junos-ops/config.ini")

    def test_default_when_no_argument_no_env(self, monkeypatch):
        """引数も環境変数もない場合はデフォルト探索"""
        monkeypatch.delenv("JUNOS_OPS_CONFIG", raising=False)
        result = _resolve_config_path("")
        # get_default_config() の結果と一致するはず
        assert result == common.get_default_config()


# --- _capture_stdout ---


class TestCaptureStdout:
    def test_captures_print_output(self):
        """print() の出力がキャプチャされる"""
        def func():
            print("hello world")
            return 42

        result, captured = _capture_stdout(func)
        assert result == 42
        assert "hello world" in captured

    def test_captures_multiple_prints(self):
        """複数の print() がすべてキャプチャされる"""
        def func():
            print("line 1")
            print("line 2")
            return 0

        result, captured = _capture_stdout(func)
        assert result == 0
        assert "line 1" in captured
        assert "line 2" in captured

    def test_passes_args_and_kwargs(self):
        """引数が正しく渡される"""
        def func(a, b, key=None):
            print(f"{a} {b} {key}")
            return a + b

        result, captured = _capture_stdout(func, 1, 2, key="test")
        assert result == 3
        assert "1 2 test" in captured

    def test_empty_stdout(self):
        """stdout に何も出力しない関数"""
        def func():
            return "no output"

        result, captured = _capture_stdout(func)
        assert result == "no output"
        assert captured == ""


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
        mock_connect.return_value = (True, None)
        result = _connect_and_run("rt1.example.jp", "", lambda h, d: "ok")
        assert "Connection" in result

    @patch("junos_ops.common.connect")
    def test_successful_operation(self, mock_connect, mock_config):
        """正常な操作の実行"""
        mock_dev = MagicMock()
        mock_connect.return_value = (False, mock_dev)
        result = _connect_and_run(
            "rt1.example.jp", "", lambda h, d: f"success: {h}"
        )
        assert result == "success: rt1.example.jp"
        mock_dev.close.assert_called_once()

    @patch("junos_ops.common.connect")
    def test_device_closed_on_exception(self, mock_connect, mock_config):
        """例外発生時もデバイス接続が閉じられる"""
        mock_dev = MagicMock()
        mock_connect.return_value = (False, mock_dev)

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
        mock_connect.return_value = (False, mock_dev)
        result = get_device_facts("rt1.example.jp")
        assert "rt1.example.jp" in result
        assert "EX2300-24T" in result
        assert "22.4R3-S6.5" in result


# --- get_version ---


class TestGetVersion:
    @patch("junos_ops.common.connect")
    @patch("junos_ops_mcp.server.upgrade.show_version")
    def test_returns_version_output(self, mock_show, mock_connect, mock_config):
        """バージョン情報を返す"""
        mock_dev = MagicMock()
        mock_connect.return_value = (False, mock_dev)
        mock_show.return_value = False
        result = get_version("rt1.example.jp")
        assert "rt1.example.jp" in result
        mock_show.assert_called_once()

    @patch("junos_ops.common.connect")
    @patch("junos_ops_mcp.server.upgrade.show_version")
    def test_shows_warning_on_error(self, mock_show, mock_connect, mock_config):
        """show_version がエラーの場合に警告を表示"""
        mock_dev = MagicMock()
        mock_connect.return_value = (False, mock_dev)
        mock_show.return_value = True  # エラー
        result = get_version("rt1.example.jp")
        assert "WARNING" in result


# --- run_show_command ---


class TestRunShowCommand:
    @patch("junos_ops.common.connect")
    def test_returns_command_output(self, mock_connect, mock_config):
        """CLI コマンドの出力を返す"""
        mock_dev = MagicMock()
        mock_dev.cli.return_value = "BGP is running\nPeer: 192.0.2.1"
        mock_connect.return_value = (False, mock_dev)
        result = run_show_command("rt1.example.jp", "show bgp summary")
        assert "rt1.example.jp" in result
        assert "show bgp summary" in result
        assert "BGP is running" in result
        mock_dev.cli.assert_called_once_with("show bgp summary")

    @patch("junos_ops.common.connect")
    def test_handles_command_error(self, mock_connect, mock_config):
        """コマンド実行エラーのハンドリング"""
        mock_dev = MagicMock()
        mock_dev.cli.side_effect = Exception("RPC error")
        mock_connect.return_value = (False, mock_dev)
        result = run_show_command("rt1.example.jp", "show invalid")
        assert "Error" in result
        assert "RPC error" in result


# --- list_remote_files ---


class TestListRemoteFiles:
    @patch("junos_ops.common.connect")
    @patch("junos_ops_mcp.server.upgrade.list_remote_path")
    def test_returns_file_list(self, mock_ls, mock_connect, mock_config):
        """ファイル一覧を返す"""
        mock_dev = MagicMock()
        mock_connect.return_value = (False, mock_dev)
        mock_ls.return_value = {"path": "/var/tmp", "files": {}}
        result = list_remote_files("rt1.example.jp")
        assert "rt1.example.jp" in result
        mock_ls.assert_called_once()

    @patch("junos_ops.common.connect")
    @patch("junos_ops_mcp.server.upgrade.list_remote_path")
    def test_restores_list_format(self, mock_ls, mock_connect, mock_config):
        """list_format が元の値に復元される"""
        mock_dev = MagicMock()
        mock_connect.return_value = (False, mock_dev)
        mock_ls.return_value = {}
        common.args.list_format = "short"
        list_remote_files("rt1.example.jp")
        assert common.args.list_format == "short"
