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
    #117 の単勝 `form.edge` と同じ定義なので、単勝と組合せの歪みを同じ
    ものさしで比べられる。

    両方に存在する組だけを返す。片方に無い組は比べようがない。
    """
    out = {}
    for k, pt in theory.items():
        pm = market.get(k)
        if not pt or not pm:
            continue
        out[k] = math.log(pt / pm)
    return out
