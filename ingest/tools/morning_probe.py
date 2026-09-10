"""朝のチェックの観測部分。数字を集めて JSON と Markdown を出す（#149）。

**このスクリプトは判断しない。** 事実を集めて並べるだけで、「効果あり/なし」も
「異常だ」も言わない。読んで判断するのは人間（と、それを助ける Claude）。

分けた理由は 2 つ:

- **AWS を読むのに鍵を持ちたくない。** GitHub Actions なら OIDC で一時
  認証情報を取れる。claude.ai のルーチンから直接 AWS を読む案（#31）は、
  アクセスキーをルーチンへ渡す経路が無くて頓挫した
- **観測と解釈を分けたい。** 数字の収集は機械的にできる。解釈は文脈が要る

出力:

- `--json` で機械可読な観測結果（差分の計算に使う）
- 既定では Markdown（issue に貼る形）

読み取りしかしない。invoke も put もしない（そもそも権限が無い）。
"""
import argparse
import calendar
import collections
import datetime
import json
import os
import sys

import boto3

REGION = "ap-northeast-1"
FUNCS = ["morning", "fetch", "archive", "read-api"]
NAME = "odds-resolver"

# 監視に使うスロット。**T-10 は使わない** — 単位時間あたりのレース密度で
# 33〜71% を正常に上下するため、閾値を置いても異常と区別できない（#145）。
# T-45 と T-15 は密度によらず 100% を保つので、崩れたら本物の異常。
WATCH_SLOTS = ["T-45", "T-15"]


def jst_today(ts=None):
    ts = ts if ts is not None else datetime.datetime.now(datetime.UTC).timestamp()
    return datetime.datetime.fromtimestamp(ts + 9 * 3600, datetime.UTC).strftime("%Y%m%d")


def _sess():
    return boto3.Session(region_name=REGION)


def lambda_metrics(sess, hours=24):
    """4 関数の Errors / Invocations。

    **データポイントが無い場合は None を返す。** 0 と混同しない。
    「エラーが 0」ではなく「一度も呼ばれていない」を意味し、両者は別の状態。
    """
    cw = sess.client("cloudwatch")
    now = datetime.datetime.now(datetime.UTC)
    out = {}
    for fn in FUNCS:
        row = {}
        for metric in ("Errors", "Invocations"):
            r = cw.get_metric_statistics(
                Namespace="AWS/Lambda", MetricName=metric,
                Dimensions=[{"Name": "FunctionName", "Value": f"{NAME}-{fn}"}],
                StartTime=now - datetime.timedelta(hours=hours), EndTime=now,
                Period=hours * 3600, Statistics=["Sum"])
            dp = r.get("Datapoints") or []
            row[metric.lower()] = dp[0]["Sum"] if dp else None
        out[fn] = row
    return out


def event_rules(sess):
    ev = sess.client("events")
    return {r["Name"]: r.get("State")
            for r in ev.list_rules().get("Rules", [])
            if r["Name"].startswith(NAME)}


def data_bucket(sess):
    """バケット名は Lambda の環境変数から引く。

    アカウント ID を含むうえ、このリポジトリは public なので直書きしない。
    """
    lam = sess.client("lambda")
    cfg = lam.get_function_configuration(FunctionName=f"{NAME}-archive")
    return cfg["Environment"]["Variables"]["DATA_BUCKET"]


def today_container(sess, date=None):
    """当日の器。会場ごとの内訳も返す。"""
    date = date or jst_today()
    ddb = sess.client("dynamodb")
    items, key = [], None
    while True:
        kw = {"TableName": f"{NAME}-hot",
              "KeyConditionExpression": "pk = :p",
              "ExpressionAttributeValues": {":p": {"S": f"DAY#{date}"}}}
        if key:
            kw["ExclusiveStartKey"] = key
        r = ddb.query(**kw)
        items.extend(r.get("Items", []))
        key = r.get("LastEvaluatedKey")
        if not key:
            break
    venues = collections.Counter(i["venue"]["S"] for i in items if "venue" in i)
    return {"date": date, "n_races": len(items), "venues": dict(venues)}


def _get_json(sess, bucket, key):
    body = sess.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def calibration(sess, bucket):
    """較正の要約と不変条件。**崩れていたら集計のバグ。**"""
    c = _get_json(sess, bucket, "calibration.json")
    tot = lambda k: sum(x["n"] for x in k)  # noqa: E731
    s = tot(c["by_surge"]["surged"])
    inv = {
        "persist_revert_eq_surged":
            tot(c["by_persistence"]["persist"]) + tot(c["by_persistence"]["revert"]) == s,
        "late_early_eq_surged":
            tot(c["by_timing"]["late"]) + tot(c["by_timing"]["early"]) == s,
        "surged_calm_eq_total":
            s + tot(c["by_surge"]["calm"]) == tot(c["total"]),
    }
    return {
        "n_days": c["n_days"], "n_races": c["n_races"],
        "n_horses": tot(c["total"]),
        "place_since": (c.get("place") or {}).get("since"),
        "invariants": inv,
        "bin_edges": c["bin_edges"],
        "by_surge": {k: [{"n": x["n"], "win_rate": x["win_rate"],
                          "payback": x["payback"]} for x in v]
                     for k, v in c["by_surge"].items()},
    }


def days(sess, bucket, limit=3):
    d = _get_json(sess, bucket, "days.json")["days"][:limit]
    return [{"date": x["date"], "n_races": x["n_races"], "venues": x["venues"]}
            for x in d]


def _list_keys(sess, bucket, prefix):
    s3, keys, token = sess.client("s3"), [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in r.get("Contents", [])]
        if not r.get("IsTruncated"):
            return keys
        token = r.get("NextContinuationToken")


def forward_progress(sess, bucket):
    """前向きログ（#106）の**貯まり具合だけ**。

    **勝率も回収率も出さない。** #106 は 2026-08-17 に判定済み
    （n=426・回収 72.2%・効果なし）で、今のログは将来の別仮説のための記録。
    途中経過の率を毎朝眺めると判断が揺れる。
    """
    keys = sorted(k for k in _list_keys(sess, bucket, "forward/")
                  if k.endswith(".json"))
    per = []
    for k in keys:
        rows = _get_json(sess, bucket, k).get("rows", [])
        per.append(sum(1 for r in rows if r.get("pos") is not None))
    n = sum(per)
    return {"days": len(keys), "n": n,
            "per_day_avg": round(n / len(per), 1) if per else 0,
            "recent": per[-5:], "latest": keys[-1].split("/")[-1] if keys else None}


def slot_health(sess, bucket, date):
    """勝負どころのスロット取得率（T-45 / T-15）。

    **T-10 は見ない**（#145）。密度で正常に上下するので警報に使えない。
    """
    keys = [k for k in _list_keys(sess, bucket, f"races/{date}")
            if k.endswith(".json")]
    if not keys:
        return {"date": date, "n_races": 0}
    have = collections.Counter()
    exotic = 0
    for k in keys:
        d = _get_json(sess, bucket, k)
        slots = {s.get("slot") for s in (d.get("snapshots") or [])}
        for w in WATCH_SLOTS:
            if w in slots:
                have[w] += 1
        if d.get("exotic"):
            exotic += 1
    n = len(keys)
    return {"date": date, "n_races": n, "exotic": exotic,
            "rates": {w: round(have[w] / n, 3) for w in WATCH_SLOTS}}


def yesterday(date_str):
    y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
    t = calendar.timegm((y, m, d, 12, 0, 0, 0, 0, 0)) - 86400
    return datetime.datetime.fromtimestamp(t, datetime.UTC).strftime("%Y%m%d")


def collect():
    sess = _sess()
    bucket = data_bucket(sess)
    today = jst_today()
    prev = yesterday(today)
    now_jst = datetime.datetime.fromtimestamp(
        datetime.datetime.now(datetime.UTC).timestamp() + 9 * 3600, datetime.UTC)
    return {
        "generated_at_jst": now_jst.strftime("%Y-%m-%d %H:%M"),
        "lambda": lambda_metrics(sess),
        "rules": event_rules(sess),
        "container": today_container(sess, today),
        "days": days(sess, bucket),
        "calibration": calibration(sess, bucket),
        "forward": forward_progress(sess, bucket),
        "slots": slot_health(sess, bucket, prev),
    }


def render(o, prev=None):
    """Markdown。**事実だけを並べ、判断は書かない。**"""
    L = [f"## 観測 {o['generated_at_jst']} JST", "", "### 稼働", "",
         "| 関数 | エラー | 呼び出し |", "|---|---|---|"]
    for fn, m in o["lambda"].items():
        f = lambda v: "未呼び出し" if v is None else f"{v:.0f}"  # noqa: E731
        L.append(f"| {fn} | {f(m['errors'])} | {f(m['invocations'])} |")
    bad = [k for k, v in o["rules"].items() if v != "ENABLED"]
    L += ["", f"- EventBridge: {len(o['rules'])} 本"
              + ("（全て ENABLED）" if not bad else f" — **{bad} が ENABLED でない**")]
    c = o["container"]
    L.append(f"- 当日の器: {c['n_races']}R {c['venues']}")
    for d in o["days"]:
        L.append(f"- 焼成済み: {d['date']} {d['n_races']}R {d['venues']}")
    cal = o["calibration"]
    ok = all(cal["invariants"].values())
    L += ["", f"- 較正: {cal['n_days']} 日 {cal['n_races']}R 延べ {cal['n_horses']}",
          f"- 不変条件: {'全て成立' if ok else '**崩れている: ' + str(cal['invariants']) + '**'}",
          f"- place.since: {cal['place_since']}"]
    s = o["slots"]
    if s.get("n_races"):
        r = " / ".join(f"{k} {v:.0%}" for k, v in s["rates"].items())
        L.append(f"- 取得（{s['date']} {s['n_races']}R）: {r} / 組合せ {s['exotic']}")
    L += ["", "### 較正の帯（急変あり）", "",
          "| 帯 | n | 勝率 | 回収 |" + (" | 前回 n |" if prev else ""),
          "|---|---|---|---|" + ("---|" if prev else "")]
    E, cur = cal["bin_edges"], cal["by_surge"]["surged"]
    old = (prev or {}).get("calibration", {}).get("by_surge", {}).get("surged")
    for i, x in enumerate(cur):
        if not x["n"]:
            continue
        row = (f"| {E[i]:.2f}-{E[i+1]:.2f} | {x['n']} | "
               f"{x['win_rate']:.1%} | {x['payback']:.1%} |")
        if prev and old:
            d = x["n"] - old[i]["n"]
            row += f" {old[i]['n']} ({d:+d}) |"
        L.append(row)
    fw = o["forward"]
    L += ["", "### 前向きログ（#106）", "",
          f"- {fw['days']} 日 / {fw['n']} 頭 / 1日平均 {fw['per_day_avg']}",
          f"- 直近5日 {fw['recent']} / 最新 {fw['latest']}",
          "", "*率は出さない（#106 は判定済み。今は将来の別仮説のための記録）*"]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="機械可読な観測結果")
    ap.add_argument("--prev", help="前回の JSON（差分の n を併記する）")
    a = ap.parse_args(argv)
    o = collect()
    if a.json:
        print(json.dumps(o, ensure_ascii=False, indent=2))
        return 0
    prev = None
    if a.prev and os.path.exists(a.prev):
        with open(a.prev) as f:
            prev = json.load(f)
    print(render(o, prev))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
