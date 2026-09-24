"""組合せ馬券の理論価格と歪み（#56）。

単勝の勝率推定（`form.race_probabilities`）から連系の理論確率を導出し、実際の
オッズとの乖離を測る。**単勝プールは比較的効率的でも、連系は組合せ空間が広く
1 点あたりの投票が薄いため大衆の価格発見が働きにくい**という仮説を検証する
（#56 本文 / Hausch–Ziemba の実証とも整合）。

## なぜ単勝と並行して測るか

#106 で単勝の急変シグナルは「効果なし」（回収 72.2%）と確定した。#117 では馬柱
軸を足して単勝の歪みを探しているが、kaz の見立てはこうだった:

> 単勝では歪みが小さすぎて検出できないが、組合せなら同じ推定精度でも歪みが
> 大きく出る

同じ `p_form`・同じ時点・同じ形の指標（対数比）で両方を測れば、**どちらに歪みが
大きいかが直接比較できる**。

## Harville 式

1 着が i である確率を p_i とすると、i が抜けた後の 2 着争いは残りの馬で
確率を再正規化したもの、と仮定する:

    P(i→j) = p_i * p_j / (1 - p_i)

強い馬が 1 着を外した後の「取りこぼし」を過小評価する既知の偏りがあるが、
**まず素朴な形で測る**。補正版（Henery 等）を先に入れると、どこまでが
モデルの効果でどこからが補正の効果か分からなくなる。
"""
import math
from itertools import permutations


def exacta_probabilities(probs: dict[int, float]) -> dict[tuple[int, int], float]:
    """馬単（1着→2着）の理論確率を Harville 式で返す。

    probs は {馬番: 勝率}。合計 1.0 を前提にする（`race_probabilities` の出力）。
    返り値の合計も 1.0 になる。
    """
    out = {}
    for i, pi in probs.items():
        rest = 1.0 - pi
        if rest <= 0:
            continue  # 1 頭が確率 1.0 を占める異常系
        for j, pj in probs.items():
            if i == j:
                continue
            out[(i, j)] = pi * pj / rest
    return out


def trio_probabilities(probs: dict[int, float]) -> dict[tuple[int, ...], float]:
    """三連複（順不同 3 頭）の理論確率。6 通りの順列を足し上げる。

    キーは昇順のタプル。三連単が要るときは `permutations` の各項を個別に
    使えばよいが、点数が跳ねるのでここでは順不同に畳んでいる。
    """
    out = {}
    nums = list(probs)
    for combo in _combinations(nums, 3):
        total = 0.0
        for i, j, k in permutations(combo):
            pi, pj, pk = probs[i], probs[j], probs[k]
            r1 = 1.0 - pi
            r2 = r1 - pj
            if r1 <= 0 or r2 <= 0:
                continue
            total += pi * (pj / r1) * (pk / r2)
        out[tuple(sorted(combo))] = total
    return out


def _combinations(items, r):
    """itertools.combinations の薄いラッパ（import を 1 か所に集める）。"""
    from itertools import combinations
    return combinations(items, r)


def market_from_odds(odds: dict) -> dict:
    """オッズを支持率に直す。合計 1.0（控除率ぶんは正規化で消える）。

    単勝の `form.market_probabilities` と同じ考え方。値が無い組（未発売・
    取消）は落とす。**落とした組は分母にも入らない**ので、残った組の中での
    相対的な支持率になる。
    """
    inv = {k: 1.0 / v for k, v in odds.items() if v}
    s = sum(inv.values())
    if s <= 0:
        return {}
    return {k: x / s for k, x in inv.items()}


def edges(theory: dict, market: dict) -> dict:
    """理論確率と市場支持率の乖離を対数比で返す。

    **正なら理論が市場より高く見ている**（市場の過小評価 = 買い候補）。
    対数比という**形**は #117 の単勝 `form.edge` と同じだが、**入力が違う**:

        単勝の edge    p_form（馬柱）  対 市場
        組合せの edge  単勝オッズ      対 組合せオッズ

    分母が違うので **σ の大小をそのまま「どちらの歪みが大きいか」と読んでは
    いけない**（#56・2026-09-19 に独立性を守る方針で確定）。組合せ側に
    `p_form` を入れないのは `docs/analysis-axes.md` の独立性の制約による。

    両方に存在する組だけを返す。片方に無い組は比べようがない。
    """
    out = {}
    for k, pt in theory.items():
        pm = market.get(k)
        if not pt or not pm:
            continue
        out[k] = math.log(pt / pm)
    return out


# 組合せの歪みの閾値（#56）。**回収率を見る前に固定した。**
#
# 2569 レース / 組合せを持つ 1201 レース / 112,262 点の分布:
#
#     平均 -0.560  σ 0.928  中央値 -0.439  正の割合 28.1%
#
# 単勝（#117）と同じく **平均 +2σ** を採る。絞り込みの程度で選んだ:
#
#   +1.0σ = +0.368  14178 点  11.81 点/レース（絞れていない）
#   +1.5σ = +0.832   3774 点   3.14 点/レース
#   +2.0σ = +1.295   1052 点   0.88 点/レース  ← これを採る
#   +2.5σ = +1.759    379 点   0.32 点/レース（n が貯まらない）
#   +3.0σ = +2.223    192 点   0.16 点/レース
#
# 1 レースあたり約 0.9 点。「これというレースを見つけて集中投資する」方針に
# 対し、ほぼ 1 レース 1 点は選び抜かれた数。単勝が 1 日 6 頭で +2σ を採った
# のと同じ判断。
#
# **上側だけを見る。** 負の側（市場が理論より高く見ている = 売り候補）は
# 点数が多い（-2σ で 3.57 点/レース）が、**買えないものを測っても仕方がない**。
# 馬券は買うことしかできないので、妙味は「市場が安く付けすぎている組」にしか
# 無い。下側を使うなら別の仮説として立てること。
#
# **分布が負に寄っているので、閾値の絶対値は正の側にある**（-0.560 + 2×0.928
# = +1.295）。つまり「理論が市場より高く見ている」組だけが候補になる。
#
# 検証期間中は動かさない。動かすなら分布から引き直して検証をやり直す。
EXOTIC_EDGE_MEAN, EXOTIC_EDGE_SD = -0.560, 0.928
EXOTIC_EDGE_THRESHOLD = EXOTIC_EDGE_MEAN + 2.0 * EXOTIC_EDGE_SD


def is_exotic_edge_pick(e: float | None) -> bool:
    """組合せの乖離が閾値を超えたか。妙味候補の判定（#56）。

    閾値は分布から決めた固定値。レースごとに再計算しないのは、その日の
    出走馬によって基準が動くと日をまたいだ比較ができなくなるため
    （`form.is_edge_pick` と同じ考え方）。
    """
    return e is not None and e >= EXOTIC_EDGE_THRESHOLD
