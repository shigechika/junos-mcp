# junos-mcp

[junos-ops](https://github.com/shigechika/junos-ops) 用の MCP サーバーです。

Juniper Networks デバイスの操作を、MCP 対応の AI アシスタント（Claude
Desktop、Claude Code など）から利用できるようにします。STDIO トランスポート
を使用します。junos-ops が人間向けの CLI ツールであるのに対し、
**junos-mcp** は同じエンジンの AI 向けインターフェースです — CLI show
コマンド、設定差分と投入、ファームウェアアップグレード、RSI/SCF 診断、
複数台をまたぐ `daily_brief` ヘルスチェックまで扱います。

## 領域別ツール

| 領域 | ツール |
|---|---|
| デバイス情報 | `get_device_facts`、`get_version`、`get_router_list`、`health_check` |
| CLI コマンド実行 | `run_show_command`、`run_show_commands`、`run_show_command_batch` |
| 設定管理 | `get_config`、`get_config_diff`、`push_config` |
| アップグレード操作 | `check_upgrade_readiness`、`compare_version`、`get_package_info`、`list_remote_files`、`copy_package`、`install_package`、`rollback_package`、`schedule_reboot` |
| 診断 | `collect_rsi`、`collect_rsi_batch` |
| プリフライトチェック | `check_reachability`、`check_local_inventory`、`check_remote_packages` |
| 日次オペレーション | `daily_brief` |

**デバイスに書き込むのは5本だけです。** `push_config`・`copy_package`・
`install_package`・`rollback_package`・`schedule_reboot`。5本とも既定は
dry-run で、それ以外はすべて読み取り専用です。各書き込みツールが実際に
何を呼び、config.ini のどの権限で通るかは
[セットアップの「状態を変えるツール」](setup.ja.md) を参照してください。

## 設計方針

**Dry-run が既定であること自体が設計の要です。** 上記5本の書き込みツールは
すべて `dry_run=True` が既定で、変更を行うには呼び出し側が明示的に
`dry_run=False` を指定する必要があります。`push_config` はさらに一歩進み、
タイムアウト付きの JUNOS `commit confirmed` を発行し、commit 後のヘルス
チェック（ping、NETCONF uptime プローブ、任意の CLI コマンド）でデバイスが
正常に戻らなければ自動的にロールバックします。

**stdout は JSON-RPC 専用のまま保たれます。** junos-ops 0.14.1 以降、コア
関数は構造化された `dict` を返し stdout には一切出力しません。MCP ツールは
代わりに `junos_ops.display.format_*()` で出力を文字列化します。
`contextlib.redirect_stdout` のラップは不要で、MCP STDIO のチャンネルに
`print()` の断片が JSON-RPC フレームへ混入することもありません。

**バッチ系ツールはモデルに届く前に出力を絞ります。**
`run_show_command_batch` はサーバーサイドの `grep_pattern` とタグによる
ホスト絞り込みを受け付けるので、数十台規模の問い合わせ（例: フリート全体の
`inet.0` 経路数）でも、生の CLI テキスト数百 KB ではなく数百バイトで返って
きます。

## 次に読むもの

- [セットアップ](setup.ja.md) — インストール、`config.ini`、環境変数、
  サーバーの登録方法、書き込みツールの権限ゲート
- [リファレンス](reference.ja.md) — 全ツール、出力形式、タグフィルタ、
  接続プール、CLI
