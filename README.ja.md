# junos-mcp

[English](README.md) | 日本語

[junos-ops](https://github.com/shigechika/junos-ops) 用の MCP (Model Context Protocol) サーバーです。

Juniper Networks デバイスの操作を、MCP 対応の AI アシスタント（Claude Desktop、Claude Code など）から利用できるようにします。STDIO トランスポートを使用します。
[junos-ops](https://github.com/shigechika/junos-ops) が人間向けの CLI ツールであるのに対し、**junos-mcp** は同じエンジンの AI 向けインターフェースです。

## 機能

### デバイス情報

| ツール | 説明 | 接続 |
|--------|------|:----:|
| `get_device_facts` | デバイス基本情報の取得（モデル、ホスト名、シリアル番号、バージョン） | 要 |
| `get_version` | JUNOS バージョン情報の表示（アップグレード状況付き） | 要 |
| `get_router_list` | config.ini に定義された全ルータの一覧表示 | 不要 |

### CLI コマンド実行

| ツール | 説明 | 接続 |
|--------|------|:----:|
| `run_show_command` | 単一の CLI show コマンドの実行 | 要 |
| `run_show_commands` | 複数の CLI コマンドを1セッションで順次実行 | 要 |
| `run_show_command_batch` | 複数デバイスに対してコマンドを並列実行 | 要 |

### 設定管理

| ツール | 説明 | 接続 |
|--------|------|:----:|
| `get_config` | デバイス設定の取得（text/set/xml 形式） | 要 |
| `get_config_diff` | rollback バージョンとの設定差分表示 | 要 |
| `push_config` | commit confirmed + ヘルスチェック付きの設定投入 | 要 |

### アップグレード操作

| ツール | 説明 | 接続 |
|--------|------|:----:|
| `check_upgrade_readiness` | アップグレード準備状況の確認 | 要 |
| `compare_version` | 2 つの JUNOS バージョン文字列の比較 | 不要 |
| `get_package_info` | モデル別パッケージファイル名とハッシュの取得 | 不要 |
| `list_remote_files` | リモートデバイスのファイル一覧表示 | 要 |
| `copy_package` | SCP によるファームウェアパッケージのコピー（チェックサム検証付き） | 要 |
| `install_package` | プリフライトチェック付きのファームウェアインストール | 要 |
| `rollback_package` | 前バージョンへのパッケージロールバック | 要 |
| `schedule_reboot` | 指定時刻でのリブートスケジュール | 要 |

### 診断

| ツール | 説明 | 接続 |
|--------|------|:----:|
| `collect_rsi` | モデル別タイムアウト付きの RSI/SCF 収集 | 要 |
| `collect_rsi_batch` | 複数デバイスからの RSI/SCF 並列収集 | 要 |

### 安全設計

すべての破壊的操作（`push_config`、`copy_package`、`install_package`、`rollback_package`、`schedule_reboot`）は **dry-run モードがデフォルト**（`dry_run=True`）です。AI アシスタントが変更を実行するには、明示的に `dry_run=False` を指定する必要があります。

`push_config` は他の Junos MCP サーバーにはない安全機能を提供します:

- **commit confirmed** — タイムアウト付き（確認されなければ自動ロールバック）
- **フォールバック付きヘルスチェック** — commit 後に ping、NETCONF uptime プローブ、または任意の CLI コマンドで確認
- **自動ロールバック** — ヘルスチェック失敗時に commit を確認せず、タイマー満了で自動ロールバック

## 必要要件

- Python 3.12 以上
- [junos-ops](https://github.com/shigechika/junos-ops) と有効な `config.ini`
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) >= 1.0

## インストール

```bash
pip install junos-mcp
```

開発用:

```bash
git clone https://github.com/shigechika/junos-mcp.git
cd junos-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

## 設定

junos-ops と同じ `config.ini` を使用します。詳細は [junos-ops README](https://github.com/shigechika/junos-ops) を参照してください。

各ツールはオプションの `config_path` パラメータを受け付けます。省略時は以下の順序で探索します:
1. 環境変数 `JUNOS_OPS_CONFIG`
2. `./config.ini`
3. `~/.config/junos-ops/config.ini`

## 使い方

### Claude Code

`claude mcp add` コマンドで MCP サーバーを登録します:

```bash
claude mcp add junos-mcp \
  -e JUNOS_OPS_CONFIG=~/.config/junos-ops/config.ini \
  -- python -m junos_mcp
```

`--scope`（`-s`）オプションで設定の保存先を選択できます:

| スコープ | 説明 | 保存先 |
|----------|------|--------|
| `local`（デフォルト） | 現在のプロジェクト、自分のみ | `~/.claude.json` |
| `project` | 現在のプロジェクト、チームで共有 | プロジェクトルートの `.mcp.json` |
| `user` | 全プロジェクト、自分のみ | `~/.claude.json` |

### Claude Desktop

Claude Desktop の設定ファイルに追加します:

| OS | 設定ファイル |
|----|-------------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "junos-mcp": {
      "command": "python",
      "args": ["-m", "junos_mcp"],
      "env": {
        "JUNOS_OPS_CONFIG": "/path/to/config.ini"
      }
    }
  }
}
```

設定変更後は Claude Desktop を再起動してください。

### MCP Inspector（開発用）

```bash
mcp dev junos_mcp/server.py
```

## テスト

```bash
pytest tests/ -v
```

19 ツール、ヘルパー関数、エッジケースをカバーする 71 テスト。

## アーキテクチャ

### stdout キャプチャ

junos-ops の関数は `print()` で結果を出力します。MCP の STDIO トランスポートは stdout を JSON-RPC 通信に使うため、`contextlib.redirect_stdout` ですべての `print()` 出力をキャプチャし、ツールの戻り値として返します。

### グローバル状態の初期化

junos-ops は `common.args` と `common.config` をグローバル変数として使用します。MCP サーバーは junos-ops のテストフィクスチャ（`conftest.py`）と同じパターンでこれらを初期化します。

### 並列実行

バッチ系ツール（`run_show_command_batch`、`collect_rsi_batch`）は junos-ops の `common.run_parallel()`（`ThreadPoolExecutor`）を使用し、`max_workers` で並列度を制御できます。

## ライセンス

Apache License 2.0
