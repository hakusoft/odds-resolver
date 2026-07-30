"""オッズ急変の検知（Issue #71）。

締切間際に支持率が急上昇した馬（= 賢い金が入った疑い）を拾う。
判定はネットワーク非依存の純粋関数に閉じ、fetch から呼ぶ。
"""

# 判定の閾値（#23 で実測調整）。5 日 216 レースのスイープで決定:
# +5pt は 1 日 66 通と多くノイズが混じり、締めると全体勝率が上がる
# （+5pt=27.9% → +8pt=33.1%）。1 日 33 通・シグナルの鋭さのバランスで
# +8pt を採る。T-20 の広い窓は残し、金額の大きさで絞る。さらに締める
# 余地あり（+10pt で 1 日 19 通・勝率 38%）。フロント（race.html）と揃える。
SURGE_MIN_SLOT = 20       # T-この分 以降のスロットだけ見る（締切間際に限定）
SURGE_DELTA = 0.08        # 支持率が前回から +これ以上 上昇したら急変


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


def _slot_minutes(slot: str | None) -> int | None:
    """スロットラベル（T-N / F）から発走までの残り分を復元する。"""
    if not slot:
        return None
    if slot == "F":
        return 0
    if slot.startswith("T-"):
        try:
            return int(slot[2:])
        except ValueError:
            return None
    return None


def surged_mask(snapshots: list, n_horses: int) -> list[bool]:
    """レースの全スナップショットから、各馬が一度でも急変したかを返す。

    分析（#76）と可視化（#73）が同じ定義を使うための集約関数。
    スナップショットには slot ラベルが要る。頭数が食い違う時点は飛ばす。
    """
    mask = [False] * n_horses
    for i in range(1, len(snapshots)):
        mn = _slot_minutes(snapshots[i].get("slot"))
        if mn is None or mn > SURGE_MIN_SLOT:
            continue
        prev = _support(snapshots[i - 1]["odds"])
        curr = _support(snapshots[i]["odds"])
        if prev is None or curr is None:
            continue
        if len(prev) != n_horses or len(curr) != n_horses:
            continue
        for j in range(n_horses):
            if curr[j] - prev[j] >= SURGE_DELTA:
                mask[j] = True
    return mask
