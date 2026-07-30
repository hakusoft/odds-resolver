"""支持率系の指標計算。fetch（書き込み時の前計算）と api（フォールバック）で共用する。"""
import math


def support_metrics(odds: list) -> tuple[float, float] | None:
    """オッズ列から (top1, ent) を返す。計算不能（全 None/0）なら None。

    top1 = 1 番人気の支持率、ent = 支持率分布の正規化エントロピー（混戦度）。
    """
    inv = [(1.0 / float(o) if o else 0.0) for o in odds]
    s = sum(inv)
    if s <= 0:
        return None
    p = sorted((x / s for x in inv), reverse=True)
    ent = -sum(x * math.log(x) for x in p if x > 0)
    ent_norm = ent / math.log(len(p)) if len(p) > 1 else 0.0
    return round(p[0], 3), round(ent_norm, 3)


# 較正曲線（#53）の支持率ビン境界。大衆の予想勝率（支持率）と実勝率を
# 帯別に突き合わせる。低支持率側を細かく切るのは favorite-longshot bias
# （大穴の過大評価）が出やすい領域を解像度高く見るため。
CALIB_BINS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.0001]


def calibration_bins(odds: list, winner_idx: int | None,
                     mask: list[bool] | None = None) -> list[dict] | None:
    """確定オッズ列と勝ち馬の index から、支持率ビンごとの寄与を返す。

    返り値は各ビンの {n, sum_support, wins, payback}。全期間で単純加算
    すると「そのビンの頭数・支持率の合計・勝った数・単勝回収（勝った
    馬のオッズ合計）」になる。平均支持率 = sum_support/n（大衆の予想
    勝率）・実勝率 = wins/n・回収率 = payback/n を突き合わせれば較正の
    ズレと妙味が出る。mask を渡すと True の馬だけ集計する（急変あり/なし
    の切り分け・#76）。オッズが全滅なら None。
    """
    inv = [(1.0 / float(o) if o else 0.0) for o in odds]
    s = sum(inv)
    if s <= 0:
        return None
    bins = [{"n": 0, "sum_support": 0.0, "wins": 0, "payback": 0.0}
            for _ in range(len(CALIB_BINS) - 1)]
    for i, x in enumerate(inv):
        if mask is not None and not mask[i]:
            continue
        sup = x / s
        b = _bin_index(sup)
        bins[b]["n"] += 1
        bins[b]["sum_support"] += sup
        if i == winner_idx:
            bins[b]["wins"] += 1
            if odds[i]:
                bins[b]["payback"] += float(odds[i])
    return bins


def _bin_index(support: float) -> int:
    for b in range(len(CALIB_BINS) - 1):
        if CALIB_BINS[b] <= support < CALIB_BINS[b + 1]:
            return b
    return len(CALIB_BINS) - 2  # ちょうど 1.0 は最終ビンへ
