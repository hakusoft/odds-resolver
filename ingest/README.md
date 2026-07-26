# ingest — 当日レース取り込み

当日の生きたレースを取得し DynamoDB へ積む Lambda 群。
`docs/race-id.md` のサイト ID と、hakusoft-infra の `odds-resolver-hot`
テーブル・EventBridge を前提にする。

| モジュール | 役割 | 対応 Issue |
| --- | --- | --- |
| `morning.py` | 0:15 JST。当日のレース表（器）を作る | #17 |
| `fetch.py` | 毎分。締切駆動の段階制で 1 レースのオッズを取る | #18 |
| `archive.py` | 23:30 JST。確定分を S3 view へ焼く | #22 |

- `source.py`: 取得元アダプタ（Crawl-Delay 60 を厳守）
- `parse.py`: HTML パーサ（ヘッダー列名基準で堅牢に）
- `venues.py`: 場コード ⇔ 会場 ⇔ スラッグ表
- `race_id.py`: サイト ID 生成

## テスト

パーサはネットワーク非依存。`tests/` の固定 HTML で検証する。
