---
name: morning-check
description: odds-resolver の朝のチェック。夜間バッチ・Lambda エラー・当日の器・較正データの健全性を確認し、前日からの差分を読む。異常があれば原因まで掘る。
argument-hint: "なし（当日を自動判定）"
allowed-tools: Bash, Read, Grep, Glob
---

# morning-check — odds-resolver の朝のチェック

> **配置について**: 正本はこのリポジトリ（`.claude/skills/morning-check/`）に置き、
> `~/.claude/skills/` からシンボリックリンクで参照する。プロジェクトスキルは
> **セッションのルート直下**の `.claude/skills/` しか読まれず、`haku/` を起点に
> 作業すると `haku/odds-resolver/.claude/` は対象外になるため。
>
> ```bash
> ln -sfn "$(git rev-parse --show-toplevel)/.claude/skills/morning-check" \
>   ~/.claude/skills/morning-check
> ```
>
> リンクなので、このファイルを更新すればスキルにもそのまま反映される。

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

4 関数すべてを見る。**エラー数と呼び出し数を並べる。**

```bash
for fn in morning fetch archive read-api; do
  q() { aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name "$1" \
    --dimensions Name=FunctionName,Value=odds-resolver-$fn \
    --start-time "$(date -u -v-24H +%Y-%m-%dT%H:%M:%S)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
    --period 86400 --statistics Sum --query 'Datapoints[0].Sum' --output text 2>/dev/null; }
  echo "  $fn: エラー $(q Errors) / 呼び出し $(q Invocations)"
done
```

**`None` を `0` に潰さないこと。** メトリクスが無い（`None`）は「エラーが 0」
ではなく「**一度も呼ばれていない**」を意味する。両者は別の状態で、
潰すと異常の見落としにつながる。

期待値:

| 関数 | 呼び出し | 意味 |
|---|---|---|
| `fetch` | 1440（毎分） | 減っていたら EventBridge か Lambda の異常 |
| `morning` / `archive` | 1〜数回 | 0 なら夜間ジョブが動いていない |
| `read-api` | **0 でも正常** | 当日ページの訪問者数。デモ段階では 0 が普通 |

`read-api` が 0 の時に本当に壊れていないか見るには、実際に叩く。
**レース ID は推測せず、DynamoDB から実在するものを取る**（存在しない ID の
404 を障害と誤読しないため）。

```bash
RID=$(aws dynamodb query --table-name odds-resolver-hot \
  --key-condition-expression "pk = :p" \
  --expression-attribute-values '{":p":{"S":"DAY#'"$(date +%Y%m%d)"'"}}' \
  --limit 1 --query 'Items[0].race_id.S' --output text)
curl -s -o /dev/null -w "  HTTP %{http_code} / %{time_total}s\n" "$CF/api/?id=$RID"
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

## 5.5 前向き検証（#106）— 貯まり具合だけを見る

較正（§5）は結果を見てから遡って集計するので、後から的を描いた可能性を
排除できない。前向き検証は**予測を結果より先に確定させた**記録で、
これだけが「勝てるか」に答えられる。

```bash
DATA=$(aws lambda get-function-configuration --function-name odds-resolver-archive \
  --query 'Environment.Variables.DATA_BUCKET' --output text)
aws s3 ls "s3://$DATA/forward/" 2>/dev/null | tail -5
```

累計を出す。**帯を切らず、全体の n だけを見る。**

```bash
python3 - <<'PY'
import json,subprocess,os
b=subprocess.run(['aws','lambda','get-function-configuration','--function-name',
  'odds-resolver-archive','--query','Environment.Variables.DATA_BUCKET',
  '--output','text'],capture_output=True,text=True).stdout.strip()
ls=subprocess.run(['aws','s3','ls',f's3://{b}/forward/'],
  capture_output=True,text=True).stdout.split()
keys=[k for k in ls if k.endswith('.json')]
n=w=0; pay=0.0
for k in keys:
    d=json.loads(subprocess.run(['aws','s3','cp',f's3://{b}/forward/{k}','-'],
      capture_output=True,text=True).stdout)
    for r in d['rows']:
        if r['pos'] is None: continue      # 着順が付いていない行は数えない
        n+=1
        if r['won']: w+=1; pay+=float(r['odds'] or 0)
print(f'日数 {len(keys)} / 記録 {n} 頭 / 的中 {w}')
if n: print(f'勝率 {w/n:.1%} / 回収 {pay/n:.1%}')
print(f'n>=300 まで あと {max(0,300-n)} 頭')
PY
```

### ここで数字を読まない

**n≥300 に達するまで、勝率も回収率も解釈しない**（#106 で先に決めた基準）。
途中経過を毎朝眺めると、良い日に「効いている」悪い日に「ダメだ」と
判断が揺れる。それを避けるために基準を先に置いた。

見るのは **貯まり具合（n）と、記録が止まっていないか**だけでよい。

到達後の判定も #106 で決めてある: 回収率が 100% を跨いだら「効果なし」に倒す。
帯の定義と急変閾値は検証期間中に動かさない（動かすなら検証はやり直し）。

**残り日数を出す。** 「まだ足りない」で終わらせず、いつ判定できるかを言う。

```bash
python3 - <<'PY'
import json,subprocess
b=subprocess.run(['aws','lambda','get-function-configuration','--function-name',
  'odds-resolver-archive','--query','Environment.Variables.DATA_BUCKET',
  '--output','text'],capture_output=True,text=True).stdout.strip()
ls=subprocess.run(['aws','s3','ls',f's3://{b}/forward/'],
  capture_output=True,text=True).stdout.split()
keys=sorted(k for k in ls if k.endswith('.json'))
per=[]
for k in keys:
    d=json.loads(subprocess.run(['aws','s3','cp',f's3://{b}/forward/{k}','-'],
      capture_output=True,text=True).stdout)
    per.append(sum(1 for r in d['rows'] if r.get('pos') is not None))
n=sum(per)
rate=n/len(per) if per else 0
print(f'累計 {n} 頭 / 1日平均 {rate:.1f} 頭')
if n < 300 and rate:
    print(f'n>=300 到達見込み: あと {(300-n)/rate:.0f} 日')
PY
```

### 読み方の原則（過去に踏んだ罠）

- **母数が増えずに率だけ動いた帯は読まない。** 1 日で数頭増えただけで
  勝率が 0%→50% に跳ねる帯がある（n=20 前後で頻発）。
- **回収率 100% 超えに飛びつかない。** #23 で「不人気×急変 回収109%」が
  閾値を締めたら消えた前例がある（n=32 の蜃気楼）。7 帯 × 複数系統を
  見ているので、偶然 100% を超えるセルは必ず出る。
- **安定している系列こそ情報。** 急変なし（n≥150）が 58-62% で動かないのは、
  標本が効いている証拠。急変あり（n≈80）の振れ幅と対比して読む。
- **複勝の回収率は下限で積んでいる**ので構造的に低く出る。単勝と直接比べない。

## 6. 今日の一手を出す

**「やることがありません」で終わらせない。**

蓄積待ちの局面で「触らない」が正解なのは **#106 の検証対象**（帯の定義・急変閾値・
前向きログ）だけ。それを動かすと検証がやり直しになる。だが分析基盤そのものは
別で、育てる余地は常にある。

分析は二軸で構成する（`docs/analysis-axes.md`）。**両軸は互いの数字を入力に
取らない** — 混ぜた瞬間その分析は市場の写像になり、市場との乖離が測れなくなる。
アイデアを出す時もこの制約を守る。

毎朝、次の 4 つの引き出しから **1 つだけ**選んで提案する。全部やろうとしない。

### 引き出し 1: 触ってよい分析（検証を汚さない）

#106 が凍結しているのは「急変シグナル × 帯」の組み合わせだけ。**別の切り口**なら
自由に試せる。

- 会場別・距離別・頭数別に較正曲線を割る（標本が足りるか確認してから）
- 時間帯（朝の窓 vs 締切前）で支持率の動きが違うかを見る
- 人気順位と支持率の乖離（同じ 20% でも 3 番人気と 5 番人気は違う）

**新しい帯を切って回収率を眺めるのは避ける。** それは #106 と同じ罠にはまる。
見るなら「分布の形」や「標本が足りるか」で、勝ち負けの数字ではない。

### 引き出し 2: 馬柱軸の下準備（オッズを見ない作業）

#117 は #56 待ちで凍結中だが、**オッズに触れない範囲の整備**は今できる。

- `records[]` の欠測率を項目別に出す（何が取れていて何が取れていないか）
- 同一馬を過去レース間で名寄せできるか確認する（馬名の表記ゆれ）
- 近走データ（`recent`）がどこまで遡れているかの実測

これらは市場データを一切見ないので、独立性の制約に抵触しない。

### 引き出し 3: 論文の棚から 1 本読む

`論文発` ラベルの Issue が積んである。**すぐ実装しなくてよい**。読んで、
今のデータで検証できるかだけ判断する。

```bash
gh issue list --repo hakusoft/odds-resolver --label 論文発 --state open \
  --json number,title -q '.[] | "  #\(.number) \(.title)"' | head -5
```

「この論文の主張は、うちのデータで測れるか / 測れないなら何が足りないか」を
1 行で書き残す。それが次の Issue の種になる。

### 引き出し 4: 溜まった小さな負債

- 未整理の Issue（軸ラベルが付いていないもの）
- README と実装の食い違い
- テストが薄い箇所

```bash
gh issue list --repo hakusoft/odds-resolver --state open --limit 40 \
  --json number,title,labels \
  -q '.[] | select((.labels|map(.name)|any(startswith("軸:")))|not)
      | select((.labels|map(.name)|index("論文発"))|not)
      | "  #\(.number) \(.title)"'
```

### 提案の形

選んだ 1 つを、**その場で着手できる粒度**まで落として書く。

> 今日の一手: 距離帯で較正曲線を割れるか、標本数だけ先に数える。
> 会場別は 15 場 × 7 帯 = 51 頭/セルで薄すぎることが分かっている（15 日
> 5364 頭時点）。距離なら 3〜4 区分に畳めるので 1 セル 200 頭前後を見込める。
> まず「区分ごとの n」だけ出して、読める厚みがあるかを判断する。
> → 30 分程度。#53 の実験場でやる。回収率は見ない。

「〜を検討する」で止めない。何を数えるか、どこでやるか、どれくらいかかるかまで。

**先に標本数だけ数える**のが安全な進め方。厚みを確認してから中身を見れば、
薄いセルの偶然に振り回されない。

## 7. 報告

事実と解釈を分けて書く。

- **稼働**: 表で（Lambda エラー / 夜間バッチ / 当日の器 / 較正の不変条件）
- **数字の動き**: 前日との差分。母数も併記する（率だけ書かない）
- **前向き検証**: n の貯まり具合と到達見込み日数（率は読まない）
- **今日の一手**: §6 で選んだ 1 つ

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
