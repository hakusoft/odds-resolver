"""組合せ馬券の歪みの分布を出す（#56 の測定ステップ）。

**このスクリプトは判断しない。** 分布を並べるだけで、「効果あり/なし」も
「儲かる」も言わない。`morning_probe` と同じ作法。

## 何を測るか

S3 の `races/*.json` を読み、締切前の単勝オッズから Harville 式で組合せの
理論確率を作り、実際の組合せオッズとの乖離（対数比）を出す。

    単勝オッズ（締切前の最終スナップショット）
      → market_probabilities で支持率に直す        ← これが理論の入力
      → exacta_probabilities / trio_probabilities  ← Harville 式
      → 実際の組合せオッズの支持率と対数比を取る   ← これが歪み

## 理論の入力に馬柱を使わない理由

`form.race_probabilities`（`p_form`）は**馬柱由来**なので、ここでは使わない。
オッズ軸の分析が馬柱を入力に取ると市場の写像になり、市場との乖離が測れなく
なる（`docs/analysis-axes.md` の独立性の制約・#56 のコメントでも確認済み）。

入力は**単勝オッズと組合せオッズだけ**。オッズ軸の内部で完結している。

測っているのは「単勝プールと組合せプールの整合性のずれ」。単勝が正しいと
仮定したときに組合せがどれだけ外れているか、であって「予測が当たるか」では
ない。

**負に寄る理由は断定できない。** 実測は平均 -0.575 で、これは「市場が理論より
平坦」を意味する（指標自体は不偏で、同じ分布同士なら厳密に 0 になる）。
ただし平坦さの原因は 2 つあり、この測定では分離できない:

1. 市場が人気薄の組を手厚く評価している（favorite-longshot bias）
2. **Harville 自身の偏り** — 強い馬が 1 着を外した後の取りこぼしを過小評価し、
   理論が上位の組に確率を寄せすぎる（`exotic.py` の docstring 参照）

2 が効いていれば「理論が尖りすぎている」だけで同じ数字になる。分離するには
補正版（Henery 等）との比較か、着順での検証が要る。**このツールは分布を
出すところまで**で、原因の断定はしない。

## 単勝の edge との比較

#56 の本題は kaz の見立ての検証:

> 単勝では歪みが小さすぎて検出できないが、組合せなら同じ推定精度でも
> 歪みが大きく出る

単勝側の実測（#56 コメント）: n=9583 平均 +0.737 σ 1.184 中央値 +0.74

**ただし単勝の edge は p_form（馬柱）対 市場**で、こちらは**単勝 対 組合せ**。
分母が違うので σ の大小をそのまま「歪みの大小」と読むことはできない。
並べて眺める材料にはなるが、結論を出すには同じ土俵の定義が要る。
この点は出力にも明記する。

使い方:

    python -m ingest.tools.exotic_edges                # 全期間
    python -m ingest.tools.exotic_edges --since 20260901
    python -m ingest.tools.exotic_edges --json
"""
import argparse
import json
import math
import os
import statistics
import sys
from concurrent import futures

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ingest.exotic import (edges, exacta_probabilities,  # noqa: E402
                           market_from_odds, trio_probabilities)
from ingest.form import market_probabilities  # noqa: E402

REGION = "ap-northeast-1"
PREFIX = "races/"

# 締切前のスナップショットとして採る最大の分。単勝と組合せを同じ時点で
# 比べたいので、組合せの取得時点（EXOTIC_SLOT_MINUTES = 10）に寄せる。
# それ以前の単勝を使うと「別の瞬間の市場」と比べることになる。
SLOT_MAX_MIN = 10


def _s3():
    return boto3.Session(region_name=REGION).client("s3")


def _parse_key(s):
    """"1-2" / "1-2-3" を int のタプルに。想定外なら None。"""
    try:
        t = tuple(int(x) for x in s.split("-"))
    except ValueError:
        return None
    return t if len(t) in (2, 3) and len(set(t)) == len(t) else None


def _last_pre_post_odds(race):
    """締切前の最終スナップショットの単勝オッズを返す。無ければ None。

    slot は "T-10" のようなラベル。T-10 以内で最も遅いものを採る。
    """
    best = None
    for s in race.get("snapshots") or []:
        slot = str(s.get("slot") or "")
        if not slot.startswith("T-"):
            continue
        try:
            m = int(slot[2:])
        except ValueError:
            continue
        if m > SLOT_MAX_MIN:
            continue
        if best is None or m < best[0]:
            best = (m, s)
    return (best[1].get("odds"), best[0]) if best else (None, None)


def race_edges_exotic(race):
    """1 レースの組合せ edge を券種ごとに返す。{kind: [edge...]}。"""
    ex = race.get("exotic") or {}
    if not ex:
        return {}
    odds, _slot = _last_pre_post_odds(race)
    if not odds:
        return {}

    horses = race.get("horses") or []
    nums = [h.get("num") for h in horses]
    pm = market_probabilities(odds)
    # 単勝の支持率を {馬番: 確率} に。取消・未発売は落とす
    probs = {n: p for n, p in zip(nums, pm) if n and p}
    if len(probs) < 3:
        return {}

    out = {}
    for kind, table in ex.items():
        parsed = {}
        for k, v in (table or {}).items():
            key = _parse_key(k)
            if key and v:
                parsed[key] = float(v)
        if not parsed:
            continue
        width = len(next(iter(parsed)))
        # **#137 の誤ラベルを読まない。** 3 頭券種が 2 頭キーで保存されていた
        # 時期（8/26-8/28）のデータは、キーが指す組が実際とは違う。件数も値も
        # それらしいので、券種名とキーの幅が食い違うことで弾く
        if kind in ("sanrenfuku", "sanrentan") and width != 3:
            continue
        if kind in ("umatan", "umafuku", "wide") and width != 2:
            continue
        if width == 2:
            theory = exacta_probabilities(probs)
        elif width == 3:
            theory = trio_probabilities(probs)
        else:
            continue
        market = market_from_odds(parsed)
        e = edges(theory, market)
        if e:
            out[kind] = list(e.values())
    return out


def _load(s3, bucket, key):
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body)
    except Exception:
        return None


def collect(bucket, since=None, limit=None):
    s3 = _s3()
    keys = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX}
        if token:
            kw["ContinuationToken"] = token
        res = s3.list_objects_v2(**kw)
        for o in res.get("Contents", []):
            k = o["Key"]
            if not k.endswith(".json"):
                continue
            date = k[len(PREFIX):len(PREFIX) + 8]
            if since and date < since:
                continue
            keys.append(k)
        token = res.get("NextContinuationToken")
        if not token:
            break
    keys.sort()
    if limit:
        keys = keys[-limit:]

    per_kind = {}
    n_races = n_with_exotic = 0
    with futures.ThreadPoolExecutor(max_workers=16) as pool:
        for race in pool.map(lambda k: _load(s3, bucket, k), keys):
            if not race:
                continue
            n_races += 1
            got = race_edges_exotic(race)
            if got:
                n_with_exotic += 1
            for kind, vals in got.items():
                per_kind.setdefault(kind, []).extend(vals)
    return {"n_races": n_races, "n_with_exotic": n_with_exotic,
            "per_kind": per_kind, "n_files": len(keys)}


def summarize(vals):
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)

    def q(p):
        return vals[min(n - 1, max(0, int(p * n)))]

    return {"n": n, "mean": statistics.fmean(vals),
            "sd": statistics.pstdev(vals) if n > 1 else 0.0,
            "median": statistics.median(vals),
            "p05": q(0.05), "p25": q(0.25), "p75": q(0.75), "p95": q(0.95),
            "min": vals[0], "max": vals[-1],
            "frac_positive": sum(1 for v in vals if v > 0) / n}


def render(res):
    L = []
    L.append("## 組合せ馬券の歪みの分布（#56）")
    L.append("")
    L.append(f"- 対象ファイル {res['n_files']} / 読めたレース {res['n_races']}")
    L.append(f"- 組合せ edge を出せたレース {res['n_with_exotic']}")
    L.append("")
    L.append("理論価格の入力は**単勝オッズと組合せオッズだけ**（馬柱は使わない）。")
    L.append("測っているのは単勝プールと組合せプールの整合性のずれ。")
    L.append("")
    for kind, vals in sorted(res["per_kind"].items()):
        s = summarize(vals)
        if not s:
            continue
        L.append(f"### {kind}")
        L.append("")
        L.append(f"```")
        L.append(f"n       {s['n']}")
        L.append(f"平均    {s['mean']:+.3f}")
        L.append(f"σ       {s['sd']:.3f}")
        L.append(f"中央値  {s['median']:+.3f}")
        L.append(f"5-95%   {s['p05']:+.3f} .. {s['p95']:+.3f}")
        L.append(f"25-75%  {s['p25']:+.3f} .. {s['p75']:+.3f}")
        L.append(f"範囲    {s['min']:+.3f} .. {s['max']:+.3f}")
        L.append(f"正の割合 {s['frac_positive']:.1%}")
        L.append(f"```")
        L.append("")
    L.append("### 単勝の edge との比較について")
    L.append("")
    L.append("単勝側の実測は n=9583 平均 +0.737 σ 1.184（#56）。**ただし定義が違う**:")
    L.append("単勝の edge は `p_form`（馬柱）対 市場、こちらは単勝 対 組合せ。")
    L.append("分母が違うので σ の大小をそのまま歪みの大小と読むことはできない。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYYMMDD 以降だけ見る")
    ap.add_argument("--limit", type=int, help="末尾 N ファイルだけ")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--bucket", default=os.environ.get(
        "DATA_BUCKET", "odds-resolver-data-417441750247"))
    a = ap.parse_args()

    res = collect(a.bucket, since=a.since, limit=a.limit)
    if a.json:
        out = {k: v for k, v in res.items() if k != "per_kind"}
        out["summary"] = {k: summarize(v) for k, v in res["per_kind"].items()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(res))


if __name__ == "__main__":
    main()
