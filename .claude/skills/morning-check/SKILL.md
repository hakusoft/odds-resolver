---
name: morning-check
description: odds-resolver の朝のチェック。夜間バッチ・Lambda エラー・当日の器・較正データの健全性を確認し、前日からの差分を読む。異常があれば原因まで掘る。
argument-hint: "なし（当日を自動判定）"
allowed-tools: Bash, Read, Grep, Glob
---

# morning-check — odds-resolver の朝のチェック

自走している取得・集計が昨夜も正しく回ったかを確認し、**数字の動きを読む**。
異常が無いことの確認だけで終わらせず、前日との差分から「何が言えるようになったか」
「まだ言えないか」を判断する。

## 前提

- **道具は `aws` / `gh` / `curl` に統一する。** MCP に依存しない（クラウドから
  回す可能性を残すため）。
- 配信物（days.json / calibration.json）は CloudFront から読む。これは
  **夜間バッチが焼いた結果を、利用者と同じ経路で見る**ことになるので、
  配信の詰まり（キャッシュ・invalidation 漏れ）も同時に検出できる。

```bash
CF=https://dpfh12i0smzxl.cloudfront.net
```

S3 の正本を直接見たい場合（配信と食い違う疑いがある時など）は、バケット名を
**動的に引く**。アカウント ID を含むうえ、このリポジトリは public なので直書きしない。

```bash
DATA=$(aws lambda get-function-configuration --function-name odds-resolver-archive \
  --query 'Environment.Variables.DATA_BUCKET' --output text)
aws s3 cp "s3://$DATA/calibration.json" - | head -c 200
```

## 1. リポジトリと未処理の PR

```bash
git checkout -q main && git pull -q && git log --oneline -3
gh pr list --repo hakusoft/odds-resolver --state open \
  --json number,title -q '.[] | "  #\(.number) \(.title)"'
gh pr list --repo hakusoft/hakusoft-infra --state open \
  --json number,title -q '.[] | "  infra#\(.number) \(.title)"'
```

マージ済みなら、そのデプロイが成功しているかも見る。

```bash
gh run list --repo hakusoft/odds-resolver --branch main --limit 3
```

## 2. Lambda のエラー（過去 24h）

4 関数すべてを見る。**期待値は全て 0.0**。

```bash
for fn in morning fetch archive read-api; do
  v=$(aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Errors \
    --dimensions Name=FunctionName,Value=odds-resolver-$fn \
    --start-time "$(date -u -v-24H +%Y-%m-%dT%H:%M:%S)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
    --period 86400 --statistics Sum --query 'Datapoints[0].Sum' --output text 2>/dev/null)
  echo "  $fn: ${v:-0.0}"
done
```

`date -u -v-24H` は **macOS(BSD date) の書式**。Linux から回す場合は
`date -u -d '24 hours ago'` に読み替える。

**0 でなければログを読む。推測しない。**

```bash
aws logs filter-log-events --log-group-name /aws/lambda/odds-resolver-<fn> \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) \
  --filter-pattern '?ERROR ?Exception' \
  --query 'events[].message' --output text | head -20
```

手動操作（recalc の試行など）の失敗もここに出る。**自分が昨日起こしたものかを
必ず確認する** — 新規の異常と取り違えない。

## 3. 夜間バッチと当日の器

```bash
# 昨日ぶんが焼かれているか（days.json の先頭が昨日の日付になる）
curl -s "$CF/data/days.json" | python3 -c "
import json,sys
for x in json.load(sys.stdin)['days'][:3]: print(' ', x['date'], x['n_races'],'R', x['venues'])
"
# 今日の器が朝ジョブ(0:15)で作られているか
aws dynamodb query --table-name odds-resolver-hot \
  --key-condition-expression "pk = :p" \
  --expression-attribute-values '{":p":{"S":"DAY#'"$(date +%Y%m%d)"'"}}' \
  --select COUNT --query Count --output text
```

器が 0 なら朝ジョブが失敗しているか、その日が非開催。**EventBridge のルールが
ENABLED かまで見る**（手動で無効化したまま忘れる事故を防ぐ）。

```bash
aws events list-rules \
  --query 'Rules[?starts_with(Name,`odds-resolver`)].[Name,State,ScheduleExpression]' \
  --output text
```

期待する 4 本（cron は UTC。JST = +9h）:

| ルール | スケジュール | JST |
|---|---|---|
| `odds-resolver-morning-daily` | `cron(15 15 * * ? *)` | 0:15 翌日の器を作る |
| `odds-resolver-fetch-minutely` | `rate(1 minute)` | 毎分 |
| `odds-resolver-archive-nightly` | `cron(30 14 * * ? *)` | 23:30 当日を焼く |
| `odds-resolver-archive-rebake` | `cron(30 17 * * ? *)` | 2:30 前日を再焼き（着順反映） |

## 4. 較正データの健全性

前日のスナップショットと比べる。**比較用の JSON を毎朝残す**のが肝
（`calibration.json` は累積の総和しか持たず、日々の変化は残らないため）。

保存先は `~/.cache/odds-resolver/morning/`。セッションをまたいで残す必要が
あるので、スクラッチパッドや `/tmp` ではなくホーム配下に置く。

```bash
SNAP=~/.cache/odds-resolver/morning
mkdir -p "$SNAP"
curl -s "$CF/data/calibration.json" -o "$SNAP/cal_$(date +%Y%m%d).json"
```

見るべき不変条件（崩れていたら集計のバグ）:

- `persist + revert == surged`
- `late + early == surged`
- `surged + calm == total`
- 複勝があれば `place.since` が出ていること

```bash
python3 - <<'PY'
import json,glob,os
fs=sorted(glob.glob(os.path.expanduser('~/.cache/odds-resolver/morning/cal_*.json')))
a=json.load(open(fs[-1]))
s=sum(x['n'] for x in a['by_surge']['surged'])
p=sum(x['n'] for x in a['by_persistence']['persist'])
r=sum(x['n'] for x in a['by_persistence']['revert'])
l=sum(x['n'] for x in a['by_timing']['late'])
e=sum(x['n'] for x in a['by_timing']['early'])
c=sum(x['n'] for x in a['by_surge']['calm'])
t=sum(x['n'] for x in a['total'])
print('日数', a['n_days'], 'レース', a['n_races'])
print('不変条件:', p+r==s, l+e==s, s+c==t)
print('place:', a.get('place',{}).get('since','なし'))
PY
```

## 5. 数字の動きを読む

**ここが本題。** 前日との差分を出し、何が言えるようになったかを判断する。

```bash
python3 - <<'PY'
import json,glob,os
fs=sorted(glob.glob(os.path.expanduser('~/.cache/odds-resolver/morning/cal_*.json')))
if len(fs) < 2:
    print('前日のスナップショットが無い（初回）。今日ぶんを保存したので明日から比較できる。')
else:
  b,a=json.load(open(fs[-2])),json.load(open(fs[-1]))
  E=a['bin_edges']
  f=lambda x: f"{x['n']:3d} {x['win_rate']:5.1%} {x['payback']:6.1%}" if x['n'] else '   -'
  for i in range(len(E)-1):
      s0,s1=b['by_surge']['surged'][i],a['by_surge']['surged'][i]
      c1=a['by_surge']['calm'][i]
      if not s1['n']: continue
      print(f"{E[i]:.2f}-{E[i+1]:.2f}".rjust(12), f(s0),'->',f(s1),' 急変なし',f(c1))
PY
```

複勝も同じ要領で前日と比べる（`place` が有る日だけ）。

### 読み方の原則（過去に踏んだ罠）

- **母数が増えずに率だけ動いた帯は読まない。** 1 日で数頭増えただけで
  勝率が 0%→50% に跳ねる帯がある（n=20 前後で頻発）。
- **回収率 100% 超えに飛びつかない。** #23 で「不人気×急変 回収109%」が
  閾値を締めたら消えた前例がある（n=32 の蜃気楼）。7 帯 × 複数系統を
  見ているので、偶然 100% を超えるセルは必ず出る。
- **安定している系列こそ情報。** 急変なし（n≥150）が 58-62% で動かないのは、
  標本が効いている証拠。急変あり（n≈80）の振れ幅と対比して読む。
- **複勝の回収率は下限で積んでいる**ので構造的に低く出る。単勝と直接比べない。

## 6. 報告

事実と解釈を分けて書く。

- **稼働**: 表で（Lambda エラー / 夜間バッチ / 当日の器 / 較正の不変条件）
- **数字の動き**: 前日との差分。母数も併記する（率だけ書かない）
- **今日やるべきこと**: 無ければ「無い」と言う。蓄積待ちの局面では
  「触らない」が正解のことが多い

**予測が外れたら明示する。** 例: 「place は今日 100% になる」と言って 91% だった
（発売前の時間帯は単複ともオッズが無いため）。外れた予測を黙って流さない。

## 付録: 過去に確認した仕様（再調査を避けるため）

- **place の欠落は正常**。T-180 以前は発売前で単勝も全頭 None。締切間際
  （T-45 以降）は 100% 揃う。較正は最終スナップショットを使うので影響なし。
- **DynamoDB の DAY 器は TTL 2 日**。3 日前の再焼き（`run`）は不可能。
  集計をやり直したい時は S3 起点の `recalc` を使う（#69）。
  ```bash
  aws lambda invoke --function-name odds-resolver-archive \
    --payload '{"mode":"recalc","date":"YYYYMMDD"}' \
    --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json
  ```
- **S3 の races/*.json には snapshots が全て残る**（7/25 のバックフィル分だけは
  確定オッズ 1 点のみ。これを全期間の姿と誤読しないこと）。
