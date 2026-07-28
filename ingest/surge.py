"""オッズ急変の検知（Issue #71）。

締切間際に支持率が急上昇した馬（= 賢い金が入った疑い）を拾う。
判定はネットワーク非依存の純粋関数に閉じ、fetch から呼ぶ。
"""

# 判定の閾値。仮置きで、実測（#23 と同様）で調整する。
SURGE_MIN_SLOT = 20       # T-この分 以降のスロットだけ見る（締切間際に限定）
SURGE_DELTA = 0.05        # 支持率が前回から +これ以上 上昇したら急変


def _support(odds: list) -> list[float] | None:
    inv = [(1.0 / o if o else 0.0) for o in odds]
    s = sum(inv)
    if s <= 0:
        return None
    return [x / s for x in inv]


def detect_surges(prev_odds: list | None, curr_odds: list,
                  horses: list, minutes_to_post: float) -> list[dict]:
    """前回と今回のオッズから、支持率が急上昇した馬を返す。

    返り値: [{num, name, prev, curr, delta}]（支持率は 0-1）。
    締切間際（T-SURGE_MIN_SLOT 以降）でなければ空。前回が無い・
    頭数が食い違う・支持率が計算不能なら空（安全側）。
    """
    if minutes_to_post > SURGE_MIN_SLOT or minutes_to_post < 0:
        return []
    if prev_odds is None or len(prev_odds) != len(curr_odds):
        return []
    prev = _support(prev_odds)
    curr = _support(curr_odds)
    if prev is None or curr is None or len(horses) != len(curr):
        return []
    out = []
    for i, h in enumerate(horses):
        delta = curr[i] - prev[i]
        if delta >= SURGE_DELTA:
            out.append({
                "num": int(h["num"]), "name": h["name"],
                "prev": round(prev[i], 3), "curr": round(curr[i], 3),
                "delta": round(delta, 3),
            })
    return out
