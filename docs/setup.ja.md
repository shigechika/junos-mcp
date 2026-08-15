# セットアップ

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

junos-ops と同じ `config.ini` を使用します — デバイスのホスト名、
ホストごと（または `[DEFAULT]`）の SSH/NETCONF 認証情報、タグ割り当てを
含みます。ファイル形式は [junos-ops README](https://github.com/shigechika/junos-ops)
を参照してください。

各ツールはオプションの `config_path` パラメータを受け付けます。省略時は
以下の順序で探索します。

1. 環境変数 `JUNOS_OPS_CONFIG`
2. `./config.ini`
3. `~/.config/junos-ops/config.ini`

`config.ini` は実質的に必須です。デバイス接続を一切開かない
`get_router_list` や `health_check` も含め、すべてのツールが起動時にこの
ファイルを読みます。ファイルが見つからないときに動作を続ける代替経路は
無いので、MCP クライアントに登録する前に、上記3か所のいずれかに有効な
`config.ini` を配置してください。

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `JUNOS_OPS_CONFIG` | 未設定 | `config.ini` のパス。未設定時は上記の探索順にフォールバック |
| `JUNOS_MCP_POOL` | `1` | `0` にすると per-host NETCONF 接続プールを無効化し、呼び出しごとに新規接続を開く |
| `JUNOS_MCP_POOL_IDLE` | `60` | プール接続のアイドルタイムアウト（秒）。`0` でアイドル退場を無効化 |
| `JUNOS_MCP_POOL_CONNECT_ATTEMPTS` | `2` | プール接続1回あたりの接続試行回数 |
| `JUNOS_MCP_POOL_CONNECT_DELAY` | `1.0` | プール接続のリトライ間隔（秒） |

## 何かに組み込む前に確認する

```bash
python -m junos_mcp --check
python -m junos_mcp --check --check-host rt1
```

`--check` は `config.ini` を読み込んで設定済みルータの一覧を表示し、
エラー時は exit 1 で終了します。`--check-host HOSTNAME` を追加すると、
そのデバイスへ実際に NETCONF セッションを開き、認証情報が通るかまで
確認できます — `--check` 単独ではインベントリファイルのパースが通ることしか
確認できません。

## 状態を変えるツール

状態を変えるのは5本だけです。ほかはすべて読み取りです。これは dry-run が
既定の5本と同じものです — dry-run と commit-confirmed の仕組みは
[Home ページの設計方針](index.ja.md) を参照してください。
このセクションは、各ツールが実際に何を呼び、デバイス側のどの権限で
ゲートされるかについてです。

| ツール | API 呼び出し | 権限ゲート |
|---|---|---|
| `push_config` | `jnpr.junos.utils.config.Config`: `lock` → `load(format="set")` → `diff` → `commit_check` → `commit(confirm=confirm_timeout)` → ヘルスチェック → 最終 `commit` → `unlock` | 対象ホストの `config.ini` アカウントに、configuration mode と commit を許す JUNOS login class が必要（読み取り専用/operator クラスでは不可）。クラス名自体はホストごとに `config.ini` で用意したもの。 |
| `copy_package` | `junos_ops.upgrade.copy()` — チェックサム検証と事前クリーンアップ付きでファームウェアパッケージを SCP コピー | 同じアカウントにファイルコピー/ストレージ書き込み権限（デバイスの flash への SCP）が必要。 |
| `install_package` | `junos_ops.upgrade.install()` — バージョン確認、保留中ロールバックの確認、コピー+チェックサム、リブートスケジュールのクリア、rescue-config 保存の後、PyEZ `SW.install()`（低容量な EX2300/EX3400 向けには `unlink` フラグ経由の `request system software add`） | ソフトウェアインストール権限が必要 — JUNOS `maintenance` クラスまたは superuser login class。 |
| `rollback_package` | `junos_ops.upgrade.rollback()` — 保留中バージョンの存在を確認した上での `request system software rollback` 相当 | `install_package` と同じ、昇格されたソフトウェアメンテナンス権限。 |
| `schedule_reboot` | `request system reboot at <time>` をスケジュール | デバイス上のリブート/メンテナンス権限が必要。 |

あるホストの `config.ini` アカウントを読み取り専用/operator クラスで
プロビジョニングすると、このホストに対してはこの5本だけが権限エラーで
失敗します。show コマンド・設定読み取り・診断・`daily_brief` など、
それ以外のツールはそのまま動作し続けます。プラグイン側に別のスイッチは
無く、この境界はすべて `config.ini` に設定した JUNOS login class に
委ねられています。

## MCP クライアントへの登録

### Claude Code（プラグイン）

このリポジトリはプラグイン 1 個のマーケットプレイスも兼ねています。

```
/plugin marketplace add shigechika/junos-mcp
/plugin install junos-mcp@junos-mcp
```

プラグインは `uvx junos-mcp` を起動し、上記の「設定」と同じ環境変数を
読みます。Claude Code を起動する前に `JUNOS_OPS_CONFIG` を export するか
（または `config.ini` を `./config.ini` や
`~/.config/junos-ops/config.ini` に配置してから）起動してください —
このファイルが省略できない理由は上記の「設定」を参照してください。

プラグインは `uvx` を起動するため、Claude Code を実行するプロセスの `PATH` に
`uvx` が通っている必要があります。ログインシェルなら通常問題ありませんが、
GUI から起動した場合は通っていないことがあります。プラグインが起動しない場合は
[uv](https://docs.astral.sh/uv/) をシステム全体にインストールしてください。

### Claude Code（手動）

```bash
claude mcp add junos-mcp \
  -e JUNOS_OPS_CONFIG=~/.config/junos-ops/config.ini \
  -- python -m junos_mcp
```

`--scope`（`-s`）オプションで設定の保存先を選択できます。

| スコープ | 説明 | 保存先 |
|---|---|---|
| `local`（デフォルト） | 現在のプロジェクト、自分のみ | `~/.claude.json` |
| `project` | 現在のプロジェクト、チームで共有 | プロジェクトルートの `.mcp.json` |
| `user` | 全プロジェクト、自分のみ | `~/.claude.json` |

### Claude Desktop

`claude_desktop_config.json`:

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

### リモートアクセス（OAuth 対応、mcp-stdio 経由）

junos-mcp は Streamable HTTP トランスポートにも対応しており、
[mcp-stdio](https://github.com/shigechika/mcp-stdio) を OAuth プロキシと
して使うことでリモートアクセスできます。

```bash
# リモートサーバー側
JUNOS_OPS_CONFIG=~/.config/junos-ops/config.ini \
  python -m junos_mcp --transport streamable-http

# ローカルマシン側
claude mcp add junos-mcp -- mcp-stdio https://your-server:8000/mcp
```

詳細な手順と OAuth プロバイダの設定は
[README.ja.md の「リモートアクセス（OAuth 対応、mcp-stdio 経由）」](https://github.com/shigechika/junos-mcp/blob/main/README.ja.md)
を参照してください。

## 次に

[リファレンス](reference.ja.md) で全ツール・出力形式・タグによるホスト
絞り込み・接続プール・CLI を扱います。
