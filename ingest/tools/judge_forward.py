"""前向き検証（#106）の判定を実行する。

較正（#53）は結果を見てから遡って集計するため、後から的を描いた可能性を
排除できない。前向きログは**予測が結果より先に確定していた**記録で、これだけが
「勝てるか」に答えられる。その判定をここで行う。

**判定日に慌てて書かない。** 数字を見てからスクリプトを書くと、書き方自体が
結果に引きずられる。先に置いておくことで「後から都合よく変えていない」ことが
git の履歴で示せる。

## #106 で先に決めた基準（変えない）

- n≥300 に達するまで結論を出さない
- 回収率が 100% を跨いだら「効果なし」に倒す（有意でないものを有りとしない）
- 帯の定義・急変閾値は検証期間中に動かさない

## 判定対象の選び方（--scope）

#106 の元の観察は「20-30% 帯 × 急変あり が回収 108.8%」で帯を限定していたが、
判断基準の n≥300 が全体を指すのか当該帯を指すのか書かれていなかった。**数字を
見てから決めると後から的を描くことになる**ので、実行時に明示させる。

- `all`  — 前向きログ全体で判定する。実運用では帯を限定せず使うので実用的。
           ただし元の 20-30% の仮説は検証されないまま残る
- `band` — 20-30% 帯だけで判定する。元の仮説に忠実だが n が貯まるのが遅い

**判定は 1 回だけ。** 「all で見てダメだったから band で見る」は禁止。それが
後から的を描く行為そのもの。

使い方:

    # n が足りているかだけ見る（率は出さない）
    python -m ingest.tools.judge_forward --scope all --check

    # 判定を実行する（基準を満たしていなければ拒否される）
    python -m ingest.tools.judge_forward --scope all
"""
import argparse
import json
import os
import sys

import boto3

# #106 で先に決めた基準。ここを緩めるなら検証をやり直す。
MIN_N = 300
BAND_LO, BAND_HI = 0.20, 0.30

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def load_rows(bucket: str) -> list[dict]:
    """forward/*.json を全部読み、着順が付いた行だけ返す。

    着順が無い行は「まだ結果が出ていない」であって外れではない。数えると
    分母だけ増えて回収率が下がる。
    """
    s3 = _get_s3()
    rows = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": "forward/"}
        if token:
            kw["ContinuationToken"] = token
        res = s3.list_objects_v2(**kw)
        for obj in res.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            for r in json.loads(body).get("rows", []):
                if r.get("pos") is not None:
                    rows.append(r)
        if not res.get("IsTruncated"):
            break
        token = res.get("NextContinuationToken")
    return rows


def select(rows: list[dict], scope: str) -> list[dict]:
    if scope == "all":
        return rows
    if scope == "band":
        return [r for r in rows
                if BAND_LO <= r.get("support", -1) < BAND_HI]
    raise ValueError(f"unknown scope: {scope}")


def tally(rows: list[dict]) -> dict:
    n = len(rows)
    wins = [r for r in rows if r.get("won")]
    payout = sum(float(r.get("odds") or 0) for r in wins)
    return {
        "n": n,
        "wins": len(wins),
        "win_rate": len(wins) / n if n else 0.0,
        "payback": payout / n if n else 0.0,
    }


def verdict(t: dict) -> str:
    """基準に照らして倒す。100% を跨いだら「効果なし」。"""
    if t["n"] < MIN_N:
        return "判定不可（n 不足）"
    # 「跨いだら効果なし」= 100% を明確に超えていなければ効果なしに倒す
    return "効果あり" if t["payback"] > 1.0 else "効果なし"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scope", required=True, choices=["all", "band"],
                    help="判定対象。数字を見る前に決めること")
    ap.add_argument("--check", action="store_true",
                    help="n が足りているかだけ見る（率を出さない）")
    a = ap.parse_args(argv)

    bucket = os.environ["DATA_BUCKET"]
    rows = select(load_rows(bucket), a.scope)
    t = tally(rows)

    if a.check:
        print(json.dumps({
            "scope": a.scope, "n": t["n"], "required": MIN_N,
            "ready": t["n"] >= MIN_N,
            "remaining": max(0, MIN_N - t["n"]),
        }, ensure_ascii=False, indent=2))
        return 0

    if t["n"] < MIN_N:
        print(f"n={t['n']} は基準 {MIN_N} に達していない。判定しない。",
              file=sys.stderr)
        print("n を貯めるか、--check で残数を見ること。", file=sys.stderr)
        return 1

    print(json.dumps({
        "scope": a.scope,
        "n": t["n"],
        "wins": t["wins"],
        "win_rate": round(t["win_rate"], 4),
        "payback": round(t["payback"], 4),
        "verdict": verdict(t),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
