# CLAUDE.md

このファイルはClaude Codeがリポジトリを理解するためのコンテキストを提供します。

## プロジェクト概要

junos-mcpは、[junos-ops](https://github.com/shigechika/junos-ops) の機能を MCP (Model Context Protocol) サーバーとして公開するパッケージです。Claude Desktop、Claude Code などの MCP 対応 AI アシスタントから Juniper Networks デバイスの操作が可能になります。

- **junos-ops**: CLI ツール + ライブラリ（人間が直接使う）
- **junos-mcp**: junos-ops を MCP サーバーとして公開（AI アシスタントが使う）

## 技術スタック

- **言語:** Python 3（3.12以上）
- **主要ライブラリ:** MCP Python SDK（FastMCP）、junos-ops（junos-eznc）
- **トランスポート:** STDIO（JSON-RPC）
- **パッケージ管理:** pyproject.toml（pip installable）
- **テスト:** pytest + モック
- **ライセンス:** Apache License 2.0

## ファイル構成

```
junos_mcp/
├── __init__.py         # パッケージ定義、__version__
├── __main__.py         # python -m junos_mcp 対応
├── pool.py             # per-host NETCONF 接続プール（ConnectionPool、get_pool）
└── server.py           # FastMCP サーバー定義、22ツール実装
tests/
├── __init__.py
├── test_pool.py                # 14 ユニットテスト（ConnectionPool）
├── test_server.py              # 78 ユニットテスト
└── test_version_consistency.py # バージョン整合性テスト
pyproject.toml          # パッケージメタデータ、依存関係
LICENSE                 # Apache License 2.0
README.md               # 英語版
README.ja.md            # 日本語版
```

## モジュール構成

### pool.py — 接続プール

- `ConnectionPool` — per-host NETCONF 接続プール。`acquire(hostname, config_path)` context manager で checkout/checkin。per-host `threading.Lock` を操作全体で保持し PyEZ `Device` のスレッドセーフ問題を回避。アイドルタイムアウト超過・`dev.connected==False` で自動退場・再接続。
- `PoolConnectionError` — 接続失敗時に `acquire()` が raise する例外。
- `get_pool()` — モジュールレベルシングルトンを lazy init して返す。`JUNOS_MCP_POOL=0` で `None` を返す（プール無効）。
- 環境変数: `JUNOS_MCP_POOL`（無効化）・`JUNOS_MCP_POOL_IDLE`（アイドルタイムアウト秒、デフォルト 60）

### server.py — MCP サーバー

#### ヘルパー関数
- `_resolve_config_path()` — config パス解決（引数 > `JUNOS_OPS_CONFIG` 環境変数 > デフォルト探索）
- `_init_globals()` — junos-ops グローバル状態の初期化（`common.args` / `common.config`）
- `_ensure_config()` — 初期化済みチェック付きの初期化ラッパー
- `_connect_and_run()` — 接続プール経由（または直接）でデバイスに接続し操作を実行

#### MCP ツール — デバイス情報（3）
- `get_device_facts` — デバイス基本情報（`dev.facts`）
- `get_version` — バージョン情報（`upgrade.show_version()`）
- `get_router_list` — config.ini の全ルータ一覧

#### MCP ツール — CLI コマンド実行（3）
- `run_show_command` — 単一 CLI コマンド実行（`dev.cli()`）
- `run_show_commands` — 複数コマンドを1セッションで順次実行
- `run_show_command_batch` — 複数デバイスに並列実行（`common.run_parallel()`）

#### MCP ツール — 設定管理（3）
- `get_config` — デバイス設定取得（text/set/xml、`dev.rpc.get_config()`）
- `get_config_diff` — rollback バージョンとの差分表示
- `push_config` — 設定投入（.set/.j2 ファイルまたはインライン、commit confirmed + ヘルスチェック）

#### MCP ツール — アップグレード操作（7）
- `check_upgrade_readiness` — アップグレード準備状況
- `compare_version` — バージョン文字列比較（デバイス接続不要）
- `get_package_info` — モデル別パッケージ情報（デバイス接続不要）
- `list_remote_files` — リモートファイル一覧
- `copy_package` — SCP によるパッケージコピー（チェックサム検証付き）
- `install_package` — パッケージインストール（プリフライトチェック付き）
- `rollback_package` — パッケージロールバック
- `schedule_reboot` — リブートスケジュール

#### MCP ツール — 診断（2）
- `collect_rsi` — RSI/SCF 収集（モデル別タイムアウト付き）
- `collect_rsi_batch` — 複数デバイスからの RSI/SCF 並列収集

#### MCP ツール — プリフライトチェック（3）
- `check_reachability` — NETCONF 到達性のみ高速確認（`junos-ops check --connect` 相当）
- `check_local_inventory` — config.ini インベントリのローカルチェックサム検証（`--local`、デバイス接続不要）
- `check_remote_packages` — デバイス側ファームウェアチェックサム検証（`--remote`）

#### 共通パラメータ
- `hostname`: 接続先ホスト名（config.ini に存在する必要あり、必須）
- `config_path`: config.ini のパス（省略時は環境変数 or デフォルト探索）

#### 安全設計
- 破壊的操作は `dry_run=True` がデフォルト
- `push_config` は commit confirmed + フォールバック付きヘルスチェック + 自動ロールバック

## 設計上の注意事項

### junos-ops の戻り値
junos-ops 0.14 以降の `format_*` API は整形済み文字列を直接返すため、MCP ツールはそれをそのまま戻り値として返せる。MCP の STDIO トランスポートは stdout を JSON-RPC 通信に使うので、**ツール実装中に `print()` を呼ばない**こと（過去は `contextlib.redirect_stdout` で回避していたが、現在は不要）。

### グローバル状態の初期化
junos-ops は `common.args`（argparse.Namespace）と `common.config`（ConfigParser）をグローバル変数として使う。サーバーは junos-ops の `conftest.py` と同パターンで初期化する。破壊的操作のツールは呼び出し前に `common.args.dry_run` 等のフラグを適切にセットする。

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

94 テスト（グローバル初期化、config パス解決、接続プール、22 ツールの動作検証、バージョン整合性）。

## バージョン管理

**単一の source of truth:** `junos_mcp/__init__.py` の `__version__` のみ。

- `pyproject.toml` は `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "junos_mcp.__version__"}` で自動参照 — 手動更新不要。
- `server.json`（MCP Registry メタデータ）の `version` と `packages[0].version` は **sentinel `"0.0.0"` に固定したプレースホルダ**。リリース時 CI（`.github/workflows/release.yml` の `mcp-registry` ジョブ）が git tag から `jq` で両フィールドを上書きするため、**コミット済みの値を手で更新する必要はない**。
- `tests/test_version_consistency.py` が `server.json` の 2 箇所の version が sentinel のままであることを assert し、誤って手動で書き換えた場合は CI red。
- リリース時の bump 手順:
  1. `junos_mcp/__init__.py` の `__version__` を更新
  2. `pytest tests/test_version_consistency.py` で sentinel が保たれているか確認
  3. コミット & タグ push → release workflow 起動（CI が server.json をタグから埋める）

## Claude Code への登録（ローカル開発）

プロジェクトスコープで登録すると、このリポジトリ内でのみ MCP サーバーが有効になる。
登録すると `.mcp.json` が生成される（ローカルパスを含むため `.gitignore` 済み）。

```bash
claude mcp add junos-mcp -s project \
  -e JUNOS_OPS_CONFIG=~/.config/junos-ops/config.ini \
  -- /path/to/junos-mcp/.venv/bin/python -m junos_mcp
```

## コーディング規約

- README.md は英語、README.ja.md は日本語
- docstring は英語、コード内コメントは日本語
- コミットメッセージは conventional commits スタイル
