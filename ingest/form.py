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
import math

# 近走が何走あれば推定してよいか。3 走未満は形が定まらない。
MIN_RUNS = 3

# 合成の重み。平均着順を主軸に、3 着内率と直近走で補正する。
W_AVG, W_TOP3, W_LAST = 0.5, 0.3, 0.2

# 素点の下限。全走最下位でも 0 にはしない。
#
# 0 を許すと race_probabilities が prob=0 を返し、「この馬は絶対に勝たない」と
# 言うことになる。実測では prob=0 の馬が 33 頭出て、うち 1 頭が実際に勝った
# （3.0%）。推定不能な馬に 0 を与えない判断（Phase 1-4）と同じ理由で、
# 素点 0 も避ける。乖離スコア（log 比）が計算できなくなる実害もある。
MIN_SCORE = 0.01


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
    raw = W_AVG * (1 - avg) + W_TOP3 * top3 + W_LAST * (1 - last)
    return max(MIN_SCORE, raw)


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


# --- 二軸の交差（#117 Phase 2） ---

def market_probabilities(odds: list) -> list[float | None]:
    """オッズ列を支持率に直す。合計は 1.0（取消・未発売は None）。

    `metrics.support_metrics` と同じ 1/odds の正規化。あちらは top1 と
    エントロピーだけを返すので、各馬の支持率が要るここで作り直している。
    """
    inv = [(1.0 / float(o) if o else 0.0) for o in odds]
    s = sum(inv)
    if s <= 0:
        return [None] * len(odds)
    return [(x / s if x > 0 else None) for x in inv]


def edge(p_form: float | None, p_market: float | None) -> float | None:
    """乖離スコア = log(p_form / p_market)。二軸の交差点（#117 Phase 2）。

    **正なら馬柱が市場より強く見ている**（市場の過小評価 = 買い候補）、
    負なら逆。0 を中心に対称。

    対数比を選んだのは、差分だと絶対量なので人気馬の小さな乖離ばかり拾い、
    「人気薄が実は走る」を取り逃すため。素の比は過小評価が 1〜∞、過大評価が
    0〜1 と非対称で閾値を置きにくい。定義は数字を見る前に固定した（#117）。

    支持率が無い馬（取消・未発売）は None。市場がまだ値を付けていない状態で
    乖離は語れない。**クリップはしない** — どこで切るかが後付けの自由度になる。
    """
    if not p_form or not p_market:
        return None
    return math.log(p_form / p_market)


def race_edges(records: list[dict], odds: list) -> list[dict]:
    """レース単位で二軸を突き合わせる。

    返すのは [{num, score, p_form, p_market, edge}]。odds の並びは records と
    同じ馬番順であることを前提にする（archive/api が揃えている）。

    **馬柱側の推定にオッズは一切入っていない。** ここが二軸が初めて出会う
    場所で、それ以前に混ざっていたら乖離を測る意味が消える。
    """
    forms = race_probabilities(records)
    market = market_probabilities(odds)
    out = []
    for i, f in enumerate(forms):
        pm = market[i] if i < len(market) else None
        out.append({**f, "p_form": f["prob"], "p_market": pm,
                    "edge": edge(f["prob"], pm)})
    return out


# 妙味ありと見なす乖離スコアの閾値（#117 Phase 2）。
#
# edge の分布は 0 中心にならない。馬柱側の推定が市場より平坦なため（市場は
# 本命に集中し裾が長い、馬柱は近走 3 項目では尖らせきれない）。実測で中央値
# +0.74、71.5% が正だった。
#
# 定義（対数比）は変えずに、**相対閾値**で絞る。1024 レース 9583 頭の分布は
# 平均 +0.737 / σ 1.184 で、平均 +2σ を採ると 1 日あたり約 6 頭になる。
#
#   +1.0σ → 49 頭/日（絞れていない）
#   +1.5σ → 20 頭/日
#   +2.0σ →  6 頭/日  ← これを採る
#   +2.5σ →  1 頭/日（検証の n が貯まらない）
#
# 「これというレースを見つけて集中投資する」という方針に対し、1 日 6 頭は
# 選び抜かれた数。**回収率を見る前に固定した。**
EDGE_MEAN, EDGE_SD = 0.737, 1.184
EDGE_THRESHOLD = EDGE_MEAN + 2.0 * EDGE_SD


def is_edge_pick(e: float | None) -> bool:
    """乖離スコアが閾値を超えたか。妙味候補の判定。

    閾値は分布から決めた固定値（EDGE_THRESHOLD）。レースごとに再計算しない
    のは、その日の出走馬によって基準が動くと日をまたいだ比較ができなくなる
    ため。分布が変わったら閾値を引き直すが、**検証期間中は動かさない**。
    """
    return e is not None and e >= EDGE_THRESHOLD
