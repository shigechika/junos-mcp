# CLAUDE.md

このファイルはClaude Codeがリポジトリを理解するためのコンテキストを提供します。

## プロジェクト概要

junos-ops-mcpは、[junos-ops](https://github.com/shigechika/junos-ops) の機能を MCP (Model Context Protocol) サーバーとして公開するパッケージです。Claude Desktop、Claude Code などの MCP 対応 AI アシスタントから Juniper Networks デバイスの操作が可能になります。

## 技術スタック

- **言語:** Python 3（3.12以上）
- **主要ライブラリ:** MCP Python SDK（FastMCP）、junos-ops（junos-eznc）
- **トランスポート:** STDIO（JSON-RPC）
- **パッケージ管理:** pyproject.toml（pip installable）
- **テスト:** pytest + モック
- **ライセンス:** Apache License 2.0

## ファイル構成

```
junos_ops_mcp/
├── __init__.py         # パッケージ定義、__version__
├── __main__.py         # python -m junos_ops_mcp 対応
└── server.py           # FastMCP サーバー定義、ツール実装
tests/
├── __init__.py
└── test_server.py      # 27 ユニットテスト
pyproject.toml          # パッケージメタデータ、依存関係
README.md               # 英語版
README.ja.md            # 日本語版
```

## モジュール構成

### server.py — MCP サーバー

#### ヘルパー関数
- `_resolve_config_path()` — config パス解決（引数 > `JUNOS_OPS_CONFIG` 環境変数 > デフォルト探索）
- `_init_globals()` — junos-ops グローバル状態の初期化（`common.args` / `common.config`）
- `_capture_stdout()` — `contextlib.redirect_stdout` で print() 出力をキャプチャ
- `_ensure_config()` — 初期化済みチェック付きの初期化ラッパー
- `_connect_and_run()` — 接続→操作→close を一元化

#### MCP ツール（Phase 1: 読み取り専用）
- `get_device_facts` — デバイス基本情報（`dev.facts`）
- `get_version` — バージョン情報（`upgrade.show_version()`）
- `run_show_command` — 任意の CLI コマンド実行（`dev.cli()`）
- `list_remote_files` — リモートファイル一覧（`upgrade.list_remote_path()`）

#### 共通パラメータ
- `hostname`: 接続先ホスト名（config.ini に存在する必要あり、必須）
- `config_path`: config.ini のパス（省略時は環境変数 or デフォルト探索）

## 設計上の注意事項

### stdout キャプチャ
junos-ops の関数は `print()` で結果を stdout に出力する。MCP の STDIO トランスポートは stdout を JSON-RPC 通信に使うため、`contextlib.redirect_stdout` で `io.StringIO` にキャプチャし、キャプチャした文字列をツールの戻り値として返す。

### グローバル状態の初期化
junos-ops は `common.args`（argparse.Namespace）と `common.config`（ConfigParser）をグローバル変数として使う。サーバーは junos-ops の `conftest.py` と同パターンで初期化する。

### config パスの解決順序
1. ツール引数の `config_path`
2. 環境変数 `JUNOS_OPS_CONFIG`
3. デフォルト探索（`./config.ini` → `~/.config/junos-ops/config.ini`）

`~` はすべての経路で `os.path.expanduser()` により展開される。

## 開発環境セットアップ

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

## テスト

```bash
pytest tests/ -v
```

27テスト（グローバル初期化、config パス解決、stdout キャプチャ、接続管理、4ツールの動作検証）。

## Claude Code への登録（ローカル開発）

```bash
claude mcp add junos-ops -s user \
  -e JUNOS_OPS_CONFIG=~/.config/junos-ops/config.ini \
  -- /path/to/junos-ops-mcp/.venv/bin/python -m junos_ops_mcp
```

## コーディング規約

- README.md は英語、README.ja.md は日本語
- docstring は英語、コード内コメントは日本語
- コミットメッセージは conventional commits スタイル
