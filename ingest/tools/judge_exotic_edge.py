"""組合せの歪み（#56）の判定を実行する。

閾値（`exotic.EXOTIC_EDGE_THRESHOLD` = +2σ）を超えた組を買った場合、控除率の
壁を超えられるかを判定する。#106 / #117 と同じ枠組みで、**予測が結果より先に
確定していた**記録（`exotic_edge/*.json`）だけを使う。

**判定日に慌てて書かない。** 数字を見てからスクリプトを書くと、書き方自体が
結果に引きずられる。先に置くことで「後から都合よく変えていない」ことが git の
履歴で示せる（#123 / #148 / judge_edge と同じやり方）。

**このファイルを置いた時点で n=0。** 前向きログは 2026-09-27 に稼働を始めた
ばかりで、判定対象の行は 1 行も存在しない。

## 先に決めた基準（変えない）

kaz が 2026-09-27 に決定。**この時点で exotic_edge ログは 1 行も無い。**

- **n>=300 に達するまで結論を出さない**
  - #106 / #117 と同じ基準にしたのは、結果を並べて比べられるようにするため
  - 閾値超えは実測 0.88 点/レース・1 日 40〜50 レースなので 1 週間強
- **券種ごとに判定する**（`--kind` で明示させる）
  - 馬単と三連複は点数もオッズ帯も違う。混ぜると何を測ったか分からなくなる
  - **「馬単でダメだったから三連複で」は禁止。** 券種ごとに 1 回だけ
- **回収率が 100% を跨いだら「効果なし」に倒す**（有意でないものを有りとしない）
- **判定は 1 回だけ。** 「全体でダメだったからオッズ帯別で見る」は禁止
- **検証期間中は閾値も理論式も動かさない**
  - 動かせば分布の前提（平均 -0.560 / σ 0.928）が変わり、検証はやり直し

## n の数え方

`exotic_edge/*.json` の全行のうち、**`hit` が None でないもの**。

`hit` が None なのは着順が足りなかったレース（取消・中止）で、「まだ結果が
出ていない」であって外れではない。数えると分母だけ増えて回収率が下がる
= 効果なし側に不当に倒れる（`judge_edge.load_rows` と同じ扱い）。

## 回収率の計算

的中した組の `odds` を足して n で割る。組合せオッズはそのまま払戻倍率なので、
単勝と同じ計算でよい。

使い方:

    export DATA_BUCKET=$(aws lambda get-function-configuration \\
      --function-name odds-resolver-archive \\
      --query 'Environment.Variables.DATA_BUCKET' --output text)

    python -m ingest.tools.judge_exotic_edge --kind umatan --check   # n だけ
    python -m ingest.tools.judge_exotic_edge --kind umatan           # 判定（1 回）
"""
import argparse
import json
import os
import sys

import boto3

# #56 で先に決めた基準。ここを緩めるなら検証をやり直す。
MIN_N = 300

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def load_rows(bucket: str, kind: str) -> list[dict]:
    """exotic_edge/*.json を全部読み、判定に使える行だけ返す。

    **hit が None の行は落とす。** 着順が足りなかったレース（取消・中止）で、
    外れではない。数えると分母だけ増えて回収率が不当に下がる。
    """
    s3 = _get_s3()
    rows = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": "exotic_edge/"}
        if token:
            kw["ContinuationToken"] = token
        res = s3.list_objects_v2(**kw)
        for obj in res.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            for r in json.loads(body).get("rows", []):
                if r.get("kind") != kind:
                    continue
                if r.get("hit") is None:
                    continue
                rows.append(r)
        if not res.get("IsTruncated"):
            break
        token = res.get("NextContinuationToken")
    return rows


def tally(rows: list[dict]) -> dict:
    """的中数と回収率。組合せオッズはそのまま払戻倍率。"""
    n = len(rows)
    hits = [r for r in rows if r.get("hit")]
    payout = sum(float(r.get("odds") or 0) for r in hits)
    return {
        "n": n,
        "hits": len(hits),
        "hit_rate": len(hits) / n if n else 0.0,
        "payback": payout / n if n else 0.0,
    }


def verdict(t: dict) -> str:
    """基準に照らして倒す。100% を跨いだら「効果なし」。"""
    if t["n"] < MIN_N:
        return "判定不可（n 不足）"
    return "効果あり" if t["payback"] > 1.0 else "効果なし"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kind", required=True,
                    choices=["umatan", "sanrenfuku", "sanrentan",
                             "umafuku", "wide"],
                    help="判定する券種（混ぜない・#56）")
    ap.add_argument("--check", action="store_true",
                    help="n が足りているかだけ見る（率を出さない）")
    ap.add_argument("--bucket", default=os.environ.get("DATA_BUCKET"))
    a = ap.parse_args(argv)

    if not a.bucket:
        print("DATA_BUCKET を渡すこと", file=sys.stderr)
        return 2

    rows = load_rows(a.bucket, a.kind)
    t = tally(rows)

    if a.check:
        print(json.dumps({
            "kind": a.kind, "n": t["n"], "required": MIN_N,
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
        "kind": a.kind,
        "n": t["n"],
        "hits": t["hits"],
        "hit_rate": round(t["hit_rate"], 4),
        "payback": round(t["payback"], 4),
        "verdict": verdict(t),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
