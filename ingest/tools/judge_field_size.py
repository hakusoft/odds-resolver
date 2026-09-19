"""頭数別に較正を割ってよいか（#148）の判定を実行する。

B(8-9頭) と C(10-11頭) で**同じ帯の実勝率がどれだけ違うか**を見る。
違わなければ「頭数による違いは無い」に倒す。

**判定日に慌てて書かない。** 数字を見てからスクリプトを書くと、書き方自体が
結果に引きずられる。先に置くことで「後から都合よく変えていない」ことが git の
履歴で示せる（#123 / judge_edge と同じやり方）。

## 先に決めた基準（変えない）

kaz が 2026-09-09 に #148 本文で決定。**この時点で両区分とも n<200。**

- **n>=200**（両区分の全 7 帯）に達するまで勝率・回収率を解釈しない
- 比較するのは **B と C の実勝率の差**。同じ帯で **3 ポイント以上**離れて
  いなければ「頭数による違いは無い」に倒す
- 区分の境界（8-9 / 10-11）は検証期間中に動かさない。動かすならやり直し
- **判定は 1 回だけ。** 「差が出なかったから区分を切り直す」は禁止

## 帯の定義を作り直さない

`ingest.metrics.calibration_bins` をそのまま使う。ここで別に切ると既存の
較正と比較できなくなる。`morning_probe.judgment_queue`（到達判定）とも
同じ関数を共有するので、**進捗と判定が違う数を見ることがない**。

## 0.00-0.05 帯について

#148 本文のとおり、この帯だけ区分間の平均支持率が 29% ズレる（多頭数ほど
極薄オッズの馬が増えるため）。**横軸が揃っていないので縦軸を比べられない。**

判定の対象からは外すが、**基準の n>=200 は全 7 帯に課す**（本文どおり）。
外すのは「差を読む対象」からであって、充足判定からではない。数字を見てから
外すと後付けになるので、ここに書いて固定する。

使い方:

    export DATA_BUCKET=$(aws lambda get-function-configuration \\
      --function-name odds-resolver-archive \\
      --query 'Environment.Variables.DATA_BUCKET' --output text)

    python -m ingest.tools.judge_field_size --check   # n の充足だけ（率は出さない）
    python -m ingest.tools.judge_field_size           # 判定（1 回だけ）
"""
import argparse
import json
import os
import sys
from concurrent import futures

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ingest.metrics import CALIB_BINS, calibration_bins  # noqa: E402

# #148 で先に決めた基準。ここを緩めるなら検証をやり直す。
MIN_N = 200
DIFF_POINTS = 3.0          # 実勝率の差がこれ未満なら「違いは無い」に倒す

# 区分の境界。検証期間中は動かさない（#148）。
GROUPS = {"B": (8, 9), "C": (10, 11)}

# 横軸が揃っていない帯。差を読む対象から外す（充足判定には含める・#148）。
EXCLUDED_BINS = [0]

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _list_keys(bucket, prefix):
    s3 = _get_s3()
    out, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        res = s3.list_objects_v2(**kw)
        out.extend(o["Key"] for o in res.get("Contents", []))
        token = res.get("NextContinuationToken")
        if not token:
            break
    return out


def _get_json(bucket, key):
    body = _get_s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def collect(bucket) -> dict:
    """区分ごと・帯ごとに {n, wins, payback} を積む。

    確定オッズ（最終スナップショット）と着順が揃ったレースだけ使う。
    `morning_probe.judgment_queue` と同じ読み方にしてあるので、進捗表示と
    判定が食い違わない。
    """
    keys = [k for k in _list_keys(bucket, "races/") if k.endswith(".json")]
    nbins = len(CALIB_BINS) - 1
    per = {g: [{"n": 0, "wins": 0, "payback": 0.0, "sum_support": 0.0}
               for _ in range(nbins)] for g in GROUPS}
    n_races = {g: 0 for g in GROUPS}

    def _one(key):
        try:
            return _get_json(bucket, key)
        except Exception:
            return None

    with futures.ThreadPoolExecutor(max_workers=16) as ex:
        for d in ex.map(_one, keys):
            if not d:
                continue
            snaps, res = d.get("snapshots") or [], d.get("result")
            if not snaps or not res:
                continue
            odds = snaps[-1].get("odds")
            if not odds:
                continue
            horses = d.get("horses") or []
            g = next((g for g, (lo, hi) in GROUPS.items()
                      if lo <= len(horses) <= hi), None)
            if g is None:
                continue
            win_num = next((r["num"] for r in res if r.get("pos") == 1), None)
            win = next((i for i, h in enumerate(horses)
                        if h.get("num") == win_num), None)
            bins = calibration_bins(odds, win)
            if bins is None:
                continue
            n_races[g] += 1
            for i, x in enumerate(bins):
                per[g][i]["n"] += x["n"]
                per[g][i]["wins"] += x["wins"]
                per[g][i]["payback"] += x["payback"]
                per[g][i]["sum_support"] += x["sum_support"]
    return {"per": per, "n_races": n_races}


def readiness(per) -> dict:
    """全 7 帯が n>=200 に達したか。**率は出さない。**"""
    out = {}
    for g, rows in per.items():
        thin = [i for i, r in enumerate(rows) if r["n"] < MIN_N]
        # **残りは最小の帯から出す。** `0.50-1.00` が律速なのは実測の性質
        # （#147）であって保証ではない。最後の帯を決め打ちにすると、別の帯が
        # 薄い時に「remaining 0 なのに ready false」という読めない出力になる
        least = min(rows, key=lambda r: r["n"])
        out[g] = {
            "limiting_band": band_label(rows.index(least)),
            "limiting_n": least["n"],
            "remaining": max(0, MIN_N - least["n"]),
            "thin_bins": len(thin),
            "ready": not thin,
        }
    out["ready"] = all(v["ready"] for v in out.values()
                       if isinstance(v, dict))
    return out


def band_label(i) -> str:
    return f"{CALIB_BINS[i]:.2f}-{CALIB_BINS[i + 1]:.2f}"


def compare(per) -> list[dict]:
    """帯ごとに B と C の実勝率を並べ、差を出す。除外帯は印をつけて残す。"""
    rows = []
    for i in range(len(CALIB_BINS) - 1):
        b, c = per["B"][i], per["C"][i]
        wb = b["wins"] / b["n"] if b["n"] else 0.0
        wc = c["wins"] / c["n"] if c["n"] else 0.0
        rows.append({
            "band": band_label(i),
            "B_n": b["n"], "B_win_rate": round(wb, 4),
            "C_n": c["n"], "C_win_rate": round(wc, 4),
            "diff_points": round((wb - wc) * 100, 2),
            "excluded": i in EXCLUDED_BINS,
        })
    return rows


def verdict(rows) -> dict:
    """先に決めた基準に照らして倒す。

    **3 ポイント以上離れた帯が 1 つも無ければ「違いは無い」。**
    除外帯（横軸が揃っていない 0.00-0.05）は判定に使わない。
    """
    judged = [r for r in rows if not r["excluded"]]
    over = [r for r in judged if abs(r["diff_points"]) >= DIFF_POINTS]
    return {
        "judged_bands": len(judged),
        "bands_over_threshold": [r["band"] for r in over],
        "max_abs_diff_points": max((abs(r["diff_points"]) for r in judged),
                                   default=0.0),
        "verdict": ("頭数による違いがある" if over
                    else "頭数による違いは無い"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="n が足りているかだけ見る（率を出さない）")
    ap.add_argument("--bucket", default=os.environ.get("DATA_BUCKET"))
    a = ap.parse_args(argv)

    if not a.bucket:
        print("DATA_BUCKET を渡すこと", file=sys.stderr)
        return 2

    got = collect(a.bucket)
    per = got["per"]
    ready = readiness(per)

    if a.check:
        print(json.dumps({"required": MIN_N, **ready},
                         ensure_ascii=False, indent=2))
        return 0

    if not ready["ready"]:
        print(f"全 7 帯が n>={MIN_N} に達していない。判定しない。",
              file=sys.stderr)
        print("--check で残数を見ること。", file=sys.stderr)
        return 1

    rows = compare(per)
    print(json.dumps({
        "min_n": MIN_N, "diff_points_threshold": DIFF_POINTS,
        "n_races": got["n_races"],
        "bands": rows,
        **verdict(rows),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
