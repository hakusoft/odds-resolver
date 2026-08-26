"""二軸の乖離（#117 Phase 3）の判定を実行する。

馬柱軸の推定勝率と市場支持率の乖離が大きい馬を買った場合、控除率の壁を
超えられるかを判定する。#106（急変シグナル）と同じ枠組みで、**予測が結果より
先に確定していた**記録だけを使う。

**判定日に慌てて書かない。** 数字を見てからスクリプトを書くと、書き方自体が
結果に引きずられる。先に置くことで「後から都合よく変えていない」ことが git の
履歴で示せる（#123 と同じやり方）。

## 先に決めた基準（変えない）

kaz が 2026-08-26 に決定。**この時点で edge ログは 1 行も存在しない。**

- **n>=300 に達するまで結論を出さない**
  - #106 と同じ基準にしたのは、結果を並べて比べられるようにするため
  - 1 日 6.9 頭ペースなので約 43 日
- **n が何を指すか**: `edge/*.json` の全行のうち、着順が付いたもの
  - #106 は「n>=300」とだけ書いて全体か帯かが曖昧で、判定直前に揉めた
  - 今回は閾値（EDGE_THRESHOLD）で絞り込み済みなので、**対象は一意**。
    帯で切り直す余地を作らない
- **単勝回収率が 100% を跨いだら「効果なし」に倒す**（有意でないものを有りとしない）
- **判定は 1 回だけ。** 「全体でダメだったから会場別で見る」は禁止
- **検証期間中は EDGE_THRESHOLD も特徴量も動かさない**
  - 動かせば分布の前提（平均 +0.737 / σ 1.184）が変わり、検証はやり直し

## 複勝について

複勝回収率も記録するが **判定には使わない**。単勝で判定し、複勝は後で別の
仮説を立てる時の材料として残すだけ。両方見て良い方を採るのは、後から的を
描く行為そのもの。

使い方:

    export DATA_BUCKET=$(aws lambda get-function-configuration \\
      --function-name odds-resolver-archive \\
      --query 'Environment.Variables.DATA_BUCKET' --output text)

    python -m ingest.tools.judge_edge --check   # n が足りたか（率は出さない）
    python -m ingest.tools.judge_edge           # 判定（1 回だけ）
"""
import argparse
import json
import os
import sys

import boto3

# #117 Phase 3 で先に決めた基準。ここを緩めるなら検証をやり直す。
MIN_N = 300

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def load_rows(bucket: str) -> list[dict]:
    """edge/*.json を全部読み、着順が付いた行だけ返す。

    着順が無い行は「まだ結果が出ていない」であって外れではない。数えると
    分母だけ増えて回収率が下がる = 効果なし側に不当に倒れる。
    """
    s3 = _get_s3()
    rows = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": "edge/"}
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


def tally(rows: list[dict]) -> dict:
    """単勝の集計。複勝は参考として併記するが判定には使わない。

    払戻は的中馬の odds を足す。edge ログは p_market を持つので、そこから
    オッズを復元する（p_market = 1/odds を正規化した値なので、控除率込みの
    元オッズには戻せない。**払戻は別途 odds を持つ必要がある**）。
    """
    n = len(rows)
    wins = [r for r in rows if r.get("won")]
    payout = sum(float(r.get("odds") or 0) for r in wins)
    top3 = [r for r in rows if r.get("top3")]
    return {
        "n": n,
        "wins": len(wins),
        "win_rate": len(wins) / n if n else 0.0,
        "payback": payout / n if n else 0.0,
        # 参考。判定には使わない
        "top3_rate": len(top3) / n if n else 0.0,
    }


def verdict(t: dict) -> str:
    """基準に照らして倒す。100% を跨いだら「効果なし」。"""
    if t["n"] < MIN_N:
        return "判定不可（n 不足）"
    return "効果あり" if t["payback"] > 1.0 else "効果なし"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="n が足りているかだけ見る（率を出さない）")
    a = ap.parse_args(argv)

    bucket = os.environ["DATA_BUCKET"]
    rows = load_rows(bucket)
    t = tally(rows)

    if a.check:
        print(json.dumps({
            "n": t["n"], "required": MIN_N,
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
        "n": t["n"],
        "wins": t["wins"],
        "win_rate": round(t["win_rate"], 4),
        "payback": round(t["payback"], 4),
        "verdict": verdict(t),
        "_reference_only": {"top3_rate": round(t["top3_rate"], 4)},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
