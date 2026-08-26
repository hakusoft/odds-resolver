# ingest — 当日レース取り込み

当日の生きたレースを取得し DynamoDB へ積む Lambda 群。
`docs/race-id.md` のサイト ID と、hakusoft-infra の `odds-resolver-hot`
テーブル・EventBridge を前提にする。

| モジュール | 役割 | 対応 Issue |
| --- | --- | --- |
| `morning.py` | 0:15 JST。当日のレース表（器）を作る | #17 |
| `fetch.py` | 毎分。スロット駆動で 1 レースのオッズを取る | #18 #47 |
| `archive.py` | 23:30 JST。確定分を S3 view へ焼く | #22 |

- `source.py`: 取得元アダプタ（Crawl-Delay 60 を厳守）
- `parse.py`: HTML パーサ（ヘッダー列名基準で堅牢に）
- `venues.py`: 場コード ⇔ 会場 ⇔ スラッグ表
- `race_id.py`: サイト ID 生成

## 取得の制約

- **Crawl-Delay 60 秒（取得元 robots.txt）は絶対制約**。フェッチャは毎分起動 ×
  1 起動 1 リクエストという構造で守る。過去ページの後追い取得でも同じ。
- **スロット駆動（#47）**: 全レース共通のスロット表（T− 分）を埋めていく。
  ベースライン = T-480〜T-60 の毎時（発売開始の 10:00 JST 以降・空振りは 30 分クールダウン）/
  勝負どころ = T-45, 30, 20, 15, 10, 8, 6, 4, 2 / 発走直後に確定 1 回（slot "F"）。
  優先は 確定 > 勝負どころ > ベースライン、同段では「超過 ÷ 許容窓」最大。
  スナップショットには slot と実取得時刻の両方を記録し、取り逃しは
  closed_slots に「閉じたが snapshot が無い」形で残る = 明示的な欠測。
  数字は仮置きで、実測調整は #23。
- **結果回収（#52）**: 朝の窓（0:16〜発売開始前）の空き分で、前日→前々日の
  未回収の着順を発走順に 1 レースずつ取る。回収済みは DAY 器の result_ok、
  空振りは result_attempt（30 分クールダウン）で追跡。2:30 JST の archive
  再焼き（mode=yesterday）で S3 view に反映される。
- 取得元の詳細（サイト名・URL・DOM 依存）は公開物に書かず、このディレクトリ内に閉じる。

## 出力データの配置

archive が焼く view と read-api は同一スキーマ（archive が api の整形関数を共用）。
フロントは「当日 = api/ 優先・過去 = data/ 優先」のフォールバックで読む。

| パス | 内容 | キャッシュ |
| --- | --- | --- |
| `data/days.json` | 開催日の目次（archive が管理） | 60 秒 |
| `data/{YYYYMMDD}/index.json` | 日別一覧 + 事前計算指標（top1 / ent） | 1 時間 |
| `data/races/{race_id}.json` | レース詳細（馬・全スナップショット・着順 `result`） | 24 時間 |
| `api/?date=` `api/?id=` | 当日の同スキーマ JSON（DynamoDB 直読み） | 60 秒 |

## tools/ — 手元から回す運用スクリプト

Lambda からは呼ばれない。**一度きりの修復**のように、夜間バッチの経路に混ぜたく
ないものを置く。

| スクリプト | 役割 | 対応 Issue |
| --- | --- | --- |
| `fix_jockey.py` | 汚染した `jockey`（斤量が入っている）を null に落とす | #118 |
| `judge_forward.py` | 急変シグナルの判定（n≥300・**判定済み: 効果なし**） | #106 |
| `judge_edge.py` | 二軸の乖離の判定（n≥300 に達したら 1 回だけ実行） | #117 |

置き場を分ける理由: `archive.recalc()` は「races/*.json は読むだけで書き換えない」
ことを前提に設計されている（#69）。S3 の焼き上がりを**書き換える**経路は夜間
バッチと性質が違うので、同じモジュールに同居させない。

書き込む系は **既定を dry-run** にし、`--apply` を明示的に付けさせる。

```bash
export DATA_BUCKET=$(aws lambda get-function-configuration \
  --function-name odds-resolver-archive \
  --query 'Environment.Variables.DATA_BUCKET' --output text)
export FRONTEND_BUCKET=$(aws lambda get-function-configuration \
  --function-name odds-resolver-archive \
  --query 'Environment.Variables.FRONTEND_BUCKET' --output text)

python -m ingest.tools.fix_jockey            # 何が変わるか見るだけ
python -m ingest.tools.fix_jockey --apply    # 実際に書く
python -m ingest.tools.fix_jockey --verify   # 両バケットを読んで残数を数える
```

書き戻す時の `Cache-Control` は `archive` の定数を import して揃える。ここだけ
値が違うと、直したファイルのキャッシュ挙動が他とズレる。

## テスト

パーサはネットワーク非依存。`tests/` の固定 HTML で検証する。

**`tests/fixtures/` は追跡していない**（取得元の実 HTML なので再配布を避ける）。
無い環境では該当テストが `pytest.skip` される。CI がまさにそれなので、
**手元と CI で通るテスト数が違う**。

```
手元（fixtures あり）: 134 passed
CI  （fixtures なし）: 130 passed, 4 skipped
```

差の 4 件はパーサの実 HTML テスト。**CI の緑はこの 4 件を検証していない**ので、
`parse.py` を触った時は手元で fixtures 込みの結果も確認する。
