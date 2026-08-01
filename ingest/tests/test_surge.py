"""オッズ急変の検知（Issue #71。ネットワーク/AWS 非依存）。"""
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.surge import detect_surges  # noqa: E402

HORSES = [{"num": 1, "name": "ア"}, {"num": 2, "name": "イ"}, {"num": 3, "name": "ウ"}]


def test_detects_support_surge_near_deadline():
    # 2番のオッズが 10 → 3 に急落 = 支持率が急上昇
    prev = [2.0, 10.0, 10.0]
    curr = [2.0, 3.0, 10.0]
    got = detect_surges(prev, curr, HORSES, minutes_to_post=8)
    assert [s["num"] for s in got] == [2]
    assert got[0]["delta"] >= 0.05


def test_no_surge_when_far_from_deadline():
    prev = [2.0, 10.0, 10.0]
    curr = [2.0, 3.0, 10.0]
    assert detect_surges(prev, curr, HORSES, minutes_to_post=40) == []


def test_no_surge_after_post():
    prev = [2.0, 10.0, 10.0]
    curr = [2.0, 3.0, 10.0]
    assert detect_surges(prev, curr, HORSES, minutes_to_post=-1) == []


def test_no_prev_is_safe():
    assert detect_surges(None, [2.0, 3.0, 10.0], HORSES, 8) == []


def test_horse_count_mismatch_is_safe():
    assert detect_surges([2.0, 3.0], [2.0, 3.0, 10.0], HORSES, 8) == []


def test_small_move_ignored():
    # わずかな変化（+5pt 未満）は拾わない
    prev = [2.0, 4.0, 4.0]
    curr = [2.0, 3.8, 4.2]
    assert detect_surges(prev, curr, HORSES, 8) == []


def _fetch(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import fetch
    importlib.reload(fetch)
    return fetch


def test_notify_records_surged_and_publishes(monkeypatch):
    f = _fetch(monkeypatch)
    published = []
    monkeypatch.setattr(f, "_publish_surges",
                        lambda race, surges, m: published.append(surges))
    race = {"pk": "DAY#x", "venue": "盛岡", "race_no": Decimal(3), "post_time": "12:00"}
    parsed = {"odds": [2.0, 3.0, 10.0],
              "horses": [{"num": 1, "name": "ア"}, {"num": 2, "name": "イ"},
                         {"num": 3, "name": "ウ"}]}
    f._notify_surges(race, [2.0, 10.0, 10.0], parsed, 8)
    assert len(published) == 1 and published[0][0]["num"] == 2
    assert race["surged"] == [Decimal(2)]


def test_notify_suppresses_duplicate(monkeypatch):
    f = _fetch(monkeypatch)
    published = []
    monkeypatch.setattr(f, "_publish_surges",
                        lambda race, surges, m: published.append(surges))
    # 既に 2番を通知済み
    race = {"venue": "盛岡", "race_no": Decimal(3), "post_time": "12:00",
            "surged": [Decimal(2)]}
    parsed = {"odds": [2.0, 3.0, 10.0],
              "horses": [{"num": 1, "name": "ア"}, {"num": 2, "name": "イ"},
                         {"num": 3, "name": "ウ"}]}
    f._notify_surges(race, [2.0, 10.0, 10.0], parsed, 8)
    assert published == []  # 重複ゆえ送らない


def test_publish_noop_without_topic(monkeypatch):
    f = _fetch(monkeypatch)
    monkeypatch.delenv("SURGE_TOPIC_ARN", raising=False)
    # トピック未設定でも例外を出さず静かに終わる
    f._publish_surges({"venue": "x", "race_no": Decimal(1), "post_time": "12:00"},
                      [{"num": 1, "name": "ア", "prev": 0.1, "curr": 0.2, "delta": 0.1}],
                      8)


def test_surged_mask_from_snapshots():
    from ingest.surge import surged_mask
    snaps = [
        {"slot": "T-45", "odds": [2.0, 10.0, 10.0]},
        {"slot": "T-8", "odds": [2.0, 3.0, 10.0]},  # 2番が急上昇
    ]
    assert surged_mask(snaps, 3) == [False, True, False]


def test_surged_mask_ignores_far_slots():
    from ingest.surge import surged_mask
    snaps = [
        {"slot": "T-45", "odds": [2.0, 10.0, 10.0]},
        {"slot": "T-30", "odds": [2.0, 3.0, 10.0]},  # T-30 は締切間際でない
    ]
    assert surged_mask(snaps, 3) == [False, False, False]


# --- 急変の持続 / 平均回帰（#88） -------------------------------------

def test_surge_event_persists_when_odds_stay_short():
    # 2番が跳ねたまま最後まで高止まり = 情報の織り込み
    from ingest.surge import persist_mask, revert_mask, surge_events
    snaps = [
        {"slot": "T-45", "odds": [2.0, 10.0, 10.0]},
        {"slot": "T-8", "odds": [2.0, 3.0, 10.0]},   # 急変
        {"slot": "F", "odds": [2.0, 3.0, 10.0]},     # 戻らない
    ]
    ev = surge_events(snaps, 3)[1]
    assert ev is not None and ev["slot"] == "T-8"
    assert ev["reverted"] is False
    assert ev["persist"] > 0
    assert persist_mask(snaps, 3) == [False, True, False]
    assert revert_mask(snaps, 3) == [False, False, False]


def test_surge_event_reverts_when_odds_drift_back():
    # 2番が跳ねた後ほぼ元へ戻る = 一時的な大口の疑い。
    # 支持率は正規化されるため 1 頭の急変は他馬も押し上げる。他馬の
    # 変動を閾値未満に収めるには頭数が要るので、以降は 8 頭で組む。
    from ingest.surge import persist_mask, revert_mask, surge_events
    base = [4.0] * 8
    mid = [4.0] * 8
    mid[1] = 2.2                                  # 2番だけ大きく短縮
    snaps = [
        {"slot": "T-45", "odds": list(base)},
        {"slot": "T-8", "odds": list(mid)},       # 急変
        {"slot": "F", "odds": list(base)},        # 元へ戻った
    ]
    ev = surge_events(snaps, 8)[1]
    assert ev is not None and ev["reverted"] is True
    assert persist_mask(snaps, 8) == [False] * 8
    assert revert_mask(snaps, 8) == [i == 1 for i in range(8)]


def test_persist_and_revert_masks_partition_surged():
    # 持続 / 回帰 は「急変あり」を重複なく覆う
    from ingest.surge import persist_mask, revert_mask, surged_mask
    base = [8.0] * 8
    mid = [8.0] * 8
    mid[1] = 2.0                                  # 2番が急変
    mid[2] = 2.0                                  # 3番も急変
    fin = [8.0] * 8
    fin[1] = 2.0                                  # 2番は高止まり
    fin[2] = 8.0                                  # 3番は戻る
    snaps = [
        {"slot": "T-45", "odds": base},
        {"slot": "T-15", "odds": mid},
        {"slot": "F", "odds": fin},
    ]
    surged = surged_mask(snaps, 8)
    persist = persist_mask(snaps, 8)
    revert = revert_mask(snaps, 8)
    assert surged == [i in (1, 2) for i in range(8)]
    assert persist == [i == 1 for i in range(8)]
    assert revert == [i == 2 for i in range(8)]
    for s, p, r in zip(surged, persist, revert):
        assert s == (p or r)      # 覆っている
        assert not (p and r)      # 重複しない


def test_surge_events_records_first_surge_only():
    # 二度跳ねても基準は最初の急変（persist は累積の残り方になる）
    from ingest.surge import surge_events
    def field(second):
        o = [8.0] * 8
        o[1] = second
        return o
    snaps = [
        {"slot": "T-18", "odds": field(8.0)},
        {"slot": "T-12", "odds": field(3.5)},   # 一度目
        {"slot": "T-4", "odds": field(2.0)},    # 二度目
        {"slot": "F", "odds": field(2.0)},
    ]
    ev = surge_events(snaps, 8)[1]
    assert ev["slot"] == "T-12"
    assert ev["before"] < ev["after"] < ev["final"]


def test_surge_events_safe_on_broken_snapshots():
    # 頭数の食い違い・オッズ全滅でも例外を出さない
    from ingest.surge import surge_events
    snaps = [
        {"slot": "T-45", "odds": [2.0, 10.0, 10.0]},
        {"slot": "T-10", "odds": [0.0, 0.0, 0.0]},   # 全滅
        {"slot": "T-8", "odds": [2.0, 3.0]},         # 頭数違い
    ]
    assert surge_events(snaps, 3) == [None, None, None]


def test_surge_events_empty_when_no_usable_snapshot():
    from ingest.surge import surge_events
    assert surge_events([], 3) == [None, None, None]


# --- 締切直前かどうか（#87） -----------------------------------------

def _field(second, n=8, base=8.0):
    o = [base] * n
    o[1] = second
    return o


def test_late_surge_inside_five_minutes():
    from ingest.surge import early_mask, late_mask, surge_events
    snaps = [
        {"slot": "T-15", "odds": _field(8.0)},
        {"slot": "T-4", "odds": _field(2.0)},   # 5分以内に急変
        {"slot": "F", "odds": _field(2.0)},
    ]
    ev = surge_events(snaps, 8)[1]
    assert ev["late"] is True
    assert late_mask(snaps, 8) == [i == 1 for i in range(8)]
    assert early_mask(snaps, 8) == [False] * 8


def test_early_surge_outside_five_minutes():
    from ingest.surge import early_mask, late_mask, surge_events
    snaps = [
        {"slot": "T-20", "odds": _field(8.0)},
        {"slot": "T-15", "odds": _field(2.0)},  # 5分より前
        {"slot": "F", "odds": _field(2.0)},
    ]
    ev = surge_events(snaps, 8)[1]
    assert ev["late"] is False
    assert late_mask(snaps, 8) == [False] * 8
    assert early_mask(snaps, 8) == [i == 1 for i in range(8)]


def test_final_slot_counts_as_late():
    # F（発走直後の確定）は残り 0 分なので late 側
    from ingest.surge import late_mask
    snaps = [
        {"slot": "T-15", "odds": _field(8.0)},
        {"slot": "F", "odds": _field(2.0)},
    ]
    assert late_mask(snaps, 8) == [i == 1 for i in range(8)]


def test_late_axis_is_independent_of_persistence():
    # 直前に跳ねて戻る馬と、早くに跳ねて持続する馬が同居しうる
    # （late と persist が独立な軸であることの確認）
    from ingest.surge import late_mask, persist_mask
    base = [8.0] * 8
    early = [8.0] * 8
    early[1] = 2.0                      # 2番: T-15 に跳ねる
    late = [8.0] * 8
    late[1] = 2.0                       # 2番は持続
    late[2] = 2.0                       # 3番: T-2 に跳ねる
    fin = [8.0] * 8
    fin[1] = 2.0                        # 2番 持続
    fin[2] = 8.0                        # 3番 戻る
    snaps = [
        {"slot": "T-20", "odds": base},
        {"slot": "T-15", "odds": early},
        {"slot": "T-2", "odds": late},
        {"slot": "F", "odds": fin},
    ]
    lm = late_mask(snaps, 8)
    pm = persist_mask(snaps, 8)
    assert lm[1] is False and pm[1] is True     # 早い × 持続
    assert lm[2] is True and pm[2] is False     # 直前 × 回帰


def test_late_mask_subset_of_surged():
    # late/early は「急変あり」の内側を割るので、和は surged に一致する
    from ingest.surge import early_mask, late_mask, surged_mask
    snaps = [
        {"slot": "T-20", "odds": _field(8.0)},
        {"slot": "T-4", "odds": _field(2.0)},
        {"slot": "F", "odds": _field(2.0)},
    ]
    s = surged_mask(snaps, 8)
    lm = late_mask(snaps, 8)
    em = early_mask(snaps, 8)
    for a, b, c in zip(s, lm, em):
        assert a == (b or c)
        assert not (b and c)
