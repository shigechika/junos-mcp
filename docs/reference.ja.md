# リファレンス

## ツール一覧

### デバイス情報

| ツール | 説明 | 接続 |
|---|---|:---:|
| `get_device_facts` | デバイス基本情報（モデル、ホスト名、シリアル番号、バージョン） | 要 |
| `get_version` | JUNOS バージョン情報（アップグレード状況付き） | 要 |
| `get_router_list` | `config.ini` に定義されたルータの一覧（タグでフィルタ可） | 不要 |
| `health_check` | サーバのバージョンと config 状態（ルータ数・タグ一覧）。軽量で、デバイスには一切接続しない | 不要 |

### CLI コマンド実行

| ツール | 説明 | 接続 |
|---|---|:---:|
| `run_show_command` | 単一の CLI show コマンドの実行（`output_format`: text/json/xml） | 要 |
| `run_show_commands` | 複数の CLI コマンドを1セッションで順次実行（`output_format`: text/json/xml） | 要 |
| `run_show_command_batch` | 複数デバイスに対してコマンドを並列実行（タグフィルタ・`grep_pattern` 対応） | 要 |

### 設定管理

| ツール | 説明 | 接続 |
|---|---|:---:|
| `get_config` | デバイス設定の取得（text/set/xml 形式） | 要 |
| `get_config_diff` | rollback バージョンとの設定差分表示 | 要 |
| `push_config` | **書き込み。** commit confirmed + ヘルスチェック付きの設定投入 | 要 |

### アップグレード操作

| ツール | 説明 | 接続 |
|---|---|:---:|
| `check_upgrade_readiness` | アップグレード準備状況の確認 | 要 |
| `compare_version` | 2 つの JUNOS バージョン文字列の比較 | 不要 |
| `get_package_info` | モデル別パッケージファイル名とハッシュの取得 | 不要 |
| `list_remote_files` | リモートデバイスのファイル一覧表示 | 要 |
| `copy_package` | **書き込み。** SCP によるファームウェアパッケージのコピー（チェックサム検証付き） | 要 |
| `install_package` | **書き込み。** プリフライトチェック付きのファームウェアインストール（EX2300/EX3400 向け `unlink` フラグ対応） | 要 |
| `rollback_package` | **書き込み。** 前バージョンへのパッケージロールバック | 要 |
| `schedule_reboot` | **書き込み。** 指定時刻でのリブートスケジュール | 要 |

### 診断

| ツール | 説明 | 接続 |
|---|---|:---:|
| `collect_rsi` | モデル別タイムアウト付きの RSI/SCF 収集 | 要 |
| `collect_rsi_batch` | 複数デバイスからの RSI/SCF 並列収集（タグでフィルタ可） | 要 |

### プリフライトチェック

`junos-ops check` サブコマンドの 3 モードに対応します。テーブル整形は
junos-ops の display 層を共有します。

| ツール | 説明 | 接続 |
|---|---|:---:|
| `check_reachability` | NETCONF 到達性 + 各ホストの空きディスク容量を確認（facts 取得なし、5 秒 TCP プローブ） | 要 |
| `check_local_inventory` | `config.ini` のインベントリに対してローカルファームウェアのチェックサムを検証 | 不要 |
| `check_remote_packages` | 配置済みファームウェアのチェックサム + デバイスの空きディスク容量を検証（SCP コピー後の検証にも） | 要 |

### 日次オペレーション

| ツール | 説明 | 接続 |
|---|---|:---:|
| `daily_brief` | 複数デバイスの朝次ヘルスチェックを並列実行。システム/シャーシアラーム、インターフェース Up/Down、指定時間内（`since_hours`、デフォルト 18 h）の syslog アラートパターン、dual-RE 故障（`[RE_FAULT]`。SRX シャーシクラスタは facts が RE 状態を誤報告するため対象外）、任意の `inet.0` 経路数ベースライン（`route_baseline`）を確認し、CRITICAL/WARNING/OK の Markdown サマリーを返す | 要 |
| `daily_brief_start` / `daily_brief_result` | 1 回の呼び出しに収まらない台数のためのバックグラウンド実行（ホスト型クライアントの 1 呼び出しの上限は約 60 秒）。`daily_brief_start` がすぐ `job_id` を返し，`daily_brief_result` を `done` になるまで呼ぶ。同期の `daily_brief` は `JUNOS_DEADLINE` 秒（既定 45，0 で無効）で打ち切り，未完了の機器を `NOT CHECKED` に列挙する。 | Yes |

書き込みを行う5本のツールが実際にデバイス上で何を呼び、`config.ini` の
どの権限でゲートされるかは [セットアップの「状態を変えるツール」](setup.ja.md)
を参照してください。

## 構造化出力形式

`run_show_command` と `run_show_commands` はオプションの `output_format`
引数を受け付けます。

| 値 | 説明 |
|---|---|
| `"text"` | デフォルト。プレーンテキストの CLI 出力（コマンドを直接入力した場合と同じ） |
| `"json"` | NETCONF JSON 出力 — デバイスが構造化された dict を返す |
| `"xml"` | NETCONF XML 出力 — デバイスが整形済み XML を返す |

CLI のパイプ（`| match`、`| last`、`| count` 等）は `output_format` に
関わらず常に無視されます。PyEZ の `Device.cli()` は NETCONF RPC 経由で
コマンドを送信するため、JUNOS 側でパイプ処理が行われません。パイプなしで
コマンドを実行し、クライアント側でフィルタするか、単一コマンドであれば
後述の `run_show_command_batch` の `grep_pattern` でサーバーサイド風の
フィルタを使ってください。

```python
run_show_command("router-a", "show bgp summary", output_format="json")
```

## サーバーサイド出力フィルタ

`run_show_command_batch` はオプションの `grep_pattern` 引数（Python `re`
パターン）を受け付けます。指定すると、各ホストの出力からパターンに
マッチした行のみが残ります。`#` で始まるヘッダー行は常に保持され、
マッチする行がないホストには `(no match)` が表示されます。93 台 ×
`show route summary` のようなバッチ結果を数百 KB から数百バイトに
削減するのはこの仕組みです。

```python
run_show_command_batch(
    command="show route summary",
    tags=["main"],
    grep_pattern=r"inet\.0:\s+\d+ destinations",
)
```

使う前に知っておくべき制約が2つあります。`run_show_command_batch` には
`output_format` 引数自体が無く、出力は常にプレーンテキストです —
`run_show_command`/`run_show_commands` のように `grep_pattern` と
`json`/`xml` 出力を組み合わせることはできません。また、1回の呼び出しで
渡せる `command` は単一の文字列で、リストではありません。

## タグによるホスト絞り込み

`run_show_command_batch`、`collect_rsi_batch`、`get_router_list` は `tags`
引数を受け付けます。文法は `junos-ops --tags` CLI フラグと同一です。

- リストの各要素が **1 つのタググループ**。グループ内のカンマ区切りタグは
  **AND** で結合。
- リスト要素同士は **OR** で結合。
- バッチ系ツールで `hostnames` と併用した場合は **積集合**。積集合が
  空ならエラーを返します。

```python
# 1 グループ・1 タグ — "main" タグを持つホスト
run_show_command_batch(command="show route summary", tags=["main"])

# 1 グループ・2 タグ — グループ内 AND: tokyo AND edge
collect_rsi_batch(tags=["tokyo,edge"])

# 2 グループ — グループ間 OR: main OR backup
get_router_list(tags=["main", "backup"])
```

`config.ini` へのタグ付け方法は
[junos-ops のタグドキュメント](https://github.com/shigechika/junos-ops#tag-based-host-filtering)
を参照してください。

## 接続プール

junos-mcp はホストごとに NETCONF 接続をプールします。アイドル状態の
`Device` を再利用することで、ツール呼び出しのたびに TCP/NETCONF
ハンドシェイクが発生しなくなります。同一ホストへの並行操作はホスト単位の
ロックでシリアライズされます。

| 環境変数 | デフォルト | 説明 |
|---|---|---|
| `JUNOS_MCP_POOL` | `1` | `0` にするとプールを無効化し、呼び出しごとに新規接続を開く |
| `JUNOS_MCP_POOL_IDLE` | `60` | アイドルタイムアウト（秒）。`0` でアイドル退場を無効化 |
| `JUNOS_MCP_POOL_CONNECT_ATTEMPTS` | `2` | プール接続1回あたりの接続試行回数 |
| `JUNOS_MCP_POOL_CONNECT_DELAY` | `1.0` | プール接続のリトライ間隔（秒） |

プール内の接続は長寿命の SSH セッションです。ポリシーでセッション継続
時間が制限されている環境では、`JUNOS_MCP_POOL_IDLE` をその制限時間より
短い値に設定するか、`JUNOS_MCP_POOL=0` でプールを無効にしてください。

## CLI

```bash
python -m junos_mcp                       # MCP サーバーとして起動（stdio・既定）
python -m junos_mcp -V                    # バージョンを表示して終了
python -m junos_mcp --check               # config.ini を読み込みルータ一覧を表示して終了
python -m junos_mcp --check --check-host rt1   # rt1 への NETCONF 認証も確認
python -m junos_mcp --transport streamable-http  # HTTP トランスポート（既定: stdio）
```

`--check` はエラー時（config が見つからない・パース失敗）exit 1 で
終了します。`--check-host HOSTNAME` を併用すると、インベントリファイルの
パースが通るだけでなく、実機に対して認証情報が通るかまで確認できます。
