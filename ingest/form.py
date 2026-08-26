"""馬柱から勝率を推定する（#117）。

分析の二軸のうち**馬柱側**。市場が何を支持しているかとは無関係に、馬そのものの
強さを推し量る。両軸が独立に出した答えが食い違うレースが「これというレース」。

**この軸はオッズを入力に取らない。** 混ぜた瞬間この軸は市場の写像になり、市場との
乖離が測れなくなる。乖離が測れなければ二軸に分けた意味が消える。詳細は
`docs/analysis-axes.md`。

## 特徴量の選び方

候補を 1 つずつ実測し、**効いたものだけ**を入れた（1024 レース / 9695 頭）。
バケツ間の勝率の開きで判断:

| 特徴量 | 開き | 採否 |
|---|---|---|
| 近走スコア（着順の正規化平均） | 7.5 倍 | 採用 |
| 近走の 3 着内率 | 4.8 倍 | 採用 |
| 直近 1 走の着順 | 4.2 倍 | 採用 |
| 前走からの間隔 | 1.4 倍 | **不採用**（効かない） |
| 騎手勝率 | 測定不能 | **不採用**（99.4% が 0。取得できていない） |
| 近走の人気 | 6.4 倍 | **不採用**（過去レースの市場評価 = 独立性に抵触） |

近走の人気は効くが入れない。**過去の市場評価を持ち込めば、この軸は市場の
写像になる。** 効くかどうかではなく、独立性で落とした。

重みは 3 特徴の性質から素朴に置いた（平均着順を主、3 着内率と直近走を従）。
**数字が良くなるまで重みを調整することはしない** — それは後から的を描く行為で、
#106 が実証した罠そのもの。
"""

# 近走が何走あれば推定してよいか。3 走未満は形が定まらない。
MIN_RUNS = 3

# 合成の重み。平均着順を主軸に、3 着内率と直近走で補正する。
W_AVG, W_TOP3, W_LAST = 0.5, 0.3, 0.2


def _usable(rec: dict) -> list[dict]:
    """着順と頭数が揃った近走だけ返す。片方でも欠ければ正規化できない。"""
    return [r for r in (rec.get("recent") or [])
            if r.get("pos") and r.get("field_size")]


def _norm_pos(run: dict) -> float:
    """着順を頭数で正規化する。0.0 = 1 着、1.0 = 最下位。

    頭数で割るのは、8 頭立ての 4 着と 16 頭立ての 4 着を同じに扱わないため。
    """
    return (run["pos"] - 1) / max(1, run["field_size"] - 1)


def form_score(rec: dict) -> float | None:
    """1 頭の強さを 0.0〜1.0 で返す。高いほど強い。近走が薄ければ None。

    None は「推定できない」であって「弱い」ではない。呼び出し側で欠測として
    扱うこと（0.0 に潰すと新馬が最弱になる）。
    """
    runs = _usable(rec)
    if len(runs) < MIN_RUNS:
        return None
    avg = sum(_norm_pos(r) for r in runs) / len(runs)
    top3 = sum(1 for r in runs if r["pos"] <= 3) / len(runs)
    last = _norm_pos(runs[0])
    return W_AVG * (1 - avg) + W_TOP3 * top3 + W_LAST * (1 - last)


def race_probabilities(records: list[dict]) -> list[dict]:
    """レース単位で推定勝率を出す。合計は 1.0 になる。

    支持率と同じ土俵に乗せるための正規化（#117 Phase 1-2）。素点のままでは
    「この馬は 0.7」と言えても市場支持率 20% と引き算できない。

    推定できない馬（近走が薄い）は素点を持たないので、**残りの確率を等分**する。
    0 を与えると新馬が「絶対に勝たない」ことになり、実態と合わない。

    返すのは [{num, score, prob}]。score は None のことがある。
    """
    scored = [(r, form_score(r)) for r in records]
    known = [(r, s) for r, s in scored if s is not None]
    unknown = [r for r, s in scored if s is None]

    if not known:
        # 全頭が推定不能。等分しか言えない
        p = 1.0 / len(records) if records else 0.0
        return [{"num": r.get("num"), "score": None, "prob": p}
                for r in records]

    total = sum(s for _, s in known)
    # 推定不能な馬には、既知馬の平均相当の重みを与えて等しく扱う
    avg = total / len(known)
    denom = total + avg * len(unknown)

    out = []
    for r, s in scored:
        w = s if s is not None else avg
        out.append({"num": r.get("num"), "score": s,
                    "prob": w / denom if denom else 0.0})
    return out
