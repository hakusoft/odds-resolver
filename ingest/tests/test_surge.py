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
