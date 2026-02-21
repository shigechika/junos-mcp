# junos-ops-mcp

[junos-ops](https://github.com/shigechika/junos-ops) 用の MCP (Model Context Protocol) サーバーです。

Juniper Networks デバイスの操作を、MCP 対応の AI アシスタント（Claude Desktop、Claude Code など）から利用できるようにします。STDIO トランスポートを使用します。

## 機能

読み取り専用のデバイス操作（Phase 1）:

| ツール | 説明 |
|--------|------|
| `get_device_facts` | デバイス基本情報の取得（モデル、ホスト名、シリアル番号、バージョン） |
| `get_version` | JUNOS バージョン情報の表示（アップグレード状況付き） |
| `run_show_command` | 任意の CLI コマンドの実行 |
| `list_remote_files` | リモートデバイスのファイル一覧表示 |

## 必要要件

- Python 3.12 以上
- [junos-ops](https://github.com/shigechika/junos-ops) と有効な `config.ini`
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) >= 1.0

## インストール

```bash
pip install junos-ops-mcp
```

開発用:

```bash
git clone https://github.com/shigechika/junos-ops-mcp.git
cd junos-ops-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

## 設定

junos-ops と同じ `config.ini` を使用します。詳細は [junos-ops README](https://github.com/shigechika/junos-ops) を参照してください。

各ツールはオプションの `config_path` パラメータを受け付けます。省略時は以下の順序で探索します:
1. `./config.ini`
2. `~/.config/junos-ops/config.ini`

## 使い方

### Claude Code

`~/.claude/settings.json` に追加:

```json
{
  "mcpServers": {
    "junos-ops": {
      "command": "/path/to/junos-ops-mcp/.venv/bin/python",
      "args": ["-m", "junos_ops_mcp.server"]
    }
  }
}
```

### Claude Desktop

Claude Desktop の設定（`claude_desktop_config.json`）に追加:

```json
{
  "mcpServers": {
    "junos-ops": {
      "command": "/path/to/junos-ops-mcp/.venv/bin/python",
      "args": ["-m", "junos_ops_mcp.server"]
    }
  }
}
```

### MCP Inspector（開発用）

```bash
mcp dev junos_ops_mcp/server.py
```

## テスト

```bash
pytest tests/ -v
```

## アーキテクチャ

### stdout キャプチャ

junos-ops の関数は `print()` で結果を出力します。MCP の STDIO トランスポートは stdout を JSON-RPC 通信に使うため、`contextlib.redirect_stdout` ですべての `print()` 出力をキャプチャし、ツールの戻り値として返します。

### グローバル状態の初期化

junos-ops は `common.args` と `common.config` をグローバル変数として使用します。MCP サーバーは junos-ops のテストフィクスチャ（`conftest.py`）と同じパターンでこれらを初期化します。

## ライセンス

Apache License 2.0
