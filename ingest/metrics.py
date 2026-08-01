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


def place_bins(odds: list, place: list, top3_idx: set,
               mask: list[bool] | None = None) -> list[dict] | None:
    """複勝の較正（#89）。ビン分けは**単勝の支持率**のまま、成績を複勝に差し替える。

    返り値は各ビンの {n, sum_support, hits, payback}。hits は 3 着以内に
    来た数、payback は的中馬の複勝オッズ（下限）の合計。

    ビンを単勝支持率で切るのは、複勝オッズから支持率を作ると分母が壊れる
    ため。3 着以内は 1 レースで 3 頭当たるので、複勝オッズの逆数和は単勝の
    約 3 倍になり（実データで 3.46 倍を確認）、単勝と同じ「確率」の意味を
    持たない。「大衆が単勝でこう評価した馬が、実際どれだけ 3 着以内に
    来たか」を測るのが目的なので、横軸は単勝支持率で揃える。

    payback に複勝オッズの**下限**を使うのは安全側に倒すため。複勝は他の
    着順の組み合わせで確定値が下限〜上限の間に決まるが、どれになるかは
    保存していない。下限で積めば回収率を過大評価しない。

    place に None が混じる馬（取得できなかった・発売前）は集計から外す。
    単勝オッズが全滅なら None。
    """
    inv = [(1.0 / float(o) if o else 0.0) for o in odds]
    s = sum(inv)
    if s <= 0:
        return None
    bins = [{"n": 0, "sum_support": 0.0, "hits": 0, "payback": 0.0}
            for _ in range(len(CALIB_BINS) - 1)]
    for i, x in enumerate(inv):
        if mask is not None and not mask[i]:
            continue
        if i >= len(place) or place[i] is None:
            continue          # 複勝が取れていない馬は分母にも入れない
        sup = x / s
        b = _bin_index(sup)
        bins[b]["n"] += 1
        bins[b]["sum_support"] += sup
        if i in top3_idx:
            bins[b]["hits"] += 1
            bins[b]["payback"] += float(place[i]["lo"])
    return bins


def _bin_index(support: float) -> int:
    for b in range(len(CALIB_BINS) - 1):
        if CALIB_BINS[b] <= support < CALIB_BINS[b + 1]:
            return b
    return len(CALIB_BINS) - 2  # ちょうど 1.0 は最終ビンへ


# 当日総括レポート（#83）の分類閾値。仮置きで実測調整の余地あり。
FIRM_TOP1 = 0.40       # 1番人気の支持率がこれ以上 = 堅い決着になりやすい
UPSET_SUPPORT = 0.10   # 勝ち馬の支持率がこれ未満 = 波乱（人気薄が勝利）
HARD_ENT = 0.85        # 混戦度 ent がこれ以上 = 難しいレース


def classify_race(race: dict) -> dict | None:
    """1 レースの詳細（最終オッズ・着順・急変）から当日総括用の分類を返す。

    {firm, upset, surge_hit, top1, ent, winner_support}。着順が無い
    （まだ終わっていない）レースは None。当日データだけで完結する。
    """
    result = race.get("result")
    snaps = race.get("snapshots")
    if not result or not snaps:
        return None
    odds = snaps[-1]["odds"]
    m = support_metrics(odds)
    if m is None:
        return None
    top1, ent = m
    inv = [(1.0 / o if o else 0.0) for o in odds]
    s = sum(inv)
    support = [x / s for x in inv]  # 馬番順の支持率

    winner_num = next((r["num"] for r in result if r["pos"] == 1), None)
    horses = race.get("horses") or []
    w_idx = next((i for i, h in enumerate(horses) if h["num"] == winner_num), None)
    winner_support = support[w_idx] if w_idx is not None and w_idx < len(support) else None

    # 1 番人気（支持率最大の馬番）が 3 着内か
    fav_idx = max(range(len(support)), key=lambda i: support[i]) if support else None
    fav_num = horses[fav_idx]["num"] if fav_idx is not None and fav_idx < len(horses) else None
    top3 = {r["num"] for r in result if r["pos"] <= 3}
    firm = fav_num in top3 if fav_num is not None else False

    upset = winner_support is not None and winner_support < UPSET_SUPPORT

    # 急変した馬が 3 着内に来たか（賢い金の的中）
    from .surge import surged_mask
    mask = surged_mask(snaps, len(horses))
    surged_nums = {horses[i]["num"] for i in range(len(horses)) if i < len(mask) and mask[i]}
    surge_hit = bool(surged_nums & top3)

    return {
        "firm": firm, "upset": upset, "surge_hit": surge_hit,
        "top1": round(top1, 3), "ent": round(ent, 3),
        "winner_support": round(winner_support, 3) if winner_support is not None else None,
    }
