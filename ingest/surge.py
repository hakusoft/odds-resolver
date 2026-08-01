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

# 「締切直前」の窓（#87 / arXiv:2509.14645）。同論文は最終オッズが同程度でも
# final-five-minute の低下があった馬は実現リターンが高いと報告する。私たちの
# スロットは T-10 以降が 2 分間隔（T-6/T-4/T-2/F）なのでこの窓が切れる。
# SURGE_MIN_SLOT を狭めるのではなく、窓の中を細分して比較するための値。
# 締めると標本が減る（#23 の前例）ので、判定を変えるのでなく軸を足す。
LATE_WINDOW = 5


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
    return [s is not None for s in surge_events(snapshots, n_horses)]


def surge_events(snapshots: list, n_horses: int) -> list[dict | None]:
    """各馬の「最初の急変」の前後を追跡した記録を返す（#88）。

    返り値は馬番順に {slot, before, after, final, persist, reverted, late} か、
    急変していなければ None。

    - before  : 急変直前の支持率
    - after   : 急変直後（跳ねた時点）の支持率
    - final   : 最終スナップショットの支持率
    - persist : final - before（跳ねがどれだけ残ったか）
    - reverted: 跳ねの半分以上が戻ったか（True = 一時的な大口の疑い）
    - late    : 締切 LATE_WINDOW 分以内に起きた急変か（#87）

    動機は arXiv:2402.02623（Betfair の情報効率性）。取引所オッズは新情報を
    速く織り込み平均回帰を示す、という報告を逆に読む: 急変が情報の織り込み
    なら跳ねは残り、単なる一時的な大口なら戻る。surged_mask は「跳ねたか」
    しか見ておらず両者を区別できないので、その先を測る。

    最初の急変を基準にするのは、そこが「情報が入った時点」だと解釈するため。
    以降に更に跳ねた分も final には含まれるので persist は累積の残り方になる。

    注意（較正に使う際）: 持続組は最終支持率が構造的に高くなる（跳ねが残った
    のだから当然）。7/30 実データ 48R では持続の最終支持率 中央値 0.37 に対し
    回帰は 0.15 だった。したがって「持続組は勝率が高い」を人気の効果と切り
    離すには支持率帯で層別する必要がある。calibration_bins に mask として
    渡す使い方（#76 と同じ形）なら帯別に割れるので、その前提で使うこと。
    """
    events: list[dict | None] = [None] * n_horses
    supports = []  # (slot_minutes, 支持率列) の時系列
    for s in snapshots:
        sup = _support(s["odds"])
        if sup is None or len(sup) != n_horses:
            supports.append(None)
            continue
        supports.append((_slot_minutes(s.get("slot")), sup))

    last = next((x for x in reversed(supports) if x is not None), None)
    if last is None:
        return events
    final = last[1]

    for i in range(1, len(supports)):
        if supports[i] is None or supports[i - 1] is None:
            continue
        mn, curr = supports[i]
        _, prev = supports[i - 1]
        if mn is None or mn > SURGE_MIN_SLOT:
            continue
        for j in range(n_horses):
            if events[j] is not None:          # 最初の急変だけを記録する
                continue
            if curr[j] - prev[j] < SURGE_DELTA:
                continue
            jump = curr[j] - prev[j]
            persist = final[j] - prev[j]
            events[j] = {
                "slot": snapshots[i].get("slot"),
                "before": round(prev[j], 3),
                "after": round(curr[j], 3),
                "final": round(final[j], 3),
                "persist": round(persist, 3),
                "reverted": persist < jump / 2,
                "late": mn <= LATE_WINDOW,
            }
    return events


def persist_mask(snapshots: list, n_horses: int) -> list[bool]:
    """急変が持続した馬だけ True（#88）。較正の切り分け用。

    「急変あり」を持続・回帰に割るための片側。もう片側は revert_mask。
    """
    return [e is not None and not e["reverted"]
            for e in surge_events(snapshots, n_horses)]


def revert_mask(snapshots: list, n_horses: int) -> list[bool]:
    """急変したが元の水準へ戻った馬だけ True（#88）。"""
    return [e is not None and e["reverted"]
            for e in surge_events(snapshots, n_horses)]


def late_mask(snapshots: list, n_horses: int) -> list[bool]:
    """締切 LATE_WINDOW 分以内に急変した馬だけ True（#87）。

    persist/revert とは**独立な軸**である点に注意。持続したかどうかと、
    直前に起きたかどうかは別の性質で、両者は交差する（早い時間に跳ねて
    持続した馬も、直前に跳ねて戻った馬もいる）。
    """
    return [e is not None and e["late"]
            for e in surge_events(snapshots, n_horses)]


def early_mask(snapshots: list, n_horses: int) -> list[bool]:
    """急変したが LATE_WINDOW より前だった馬だけ True（#87）。"""
    return [e is not None and not e["late"]
            for e in surge_events(snapshots, n_horses)]
