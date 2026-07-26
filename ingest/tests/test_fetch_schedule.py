"""フェッチャのスロット駆動スケジューリング（ネットワーク/DynamoDB 非依存）。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


def _mod(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import fetch
    importlib.reload(fetch)
    monkeypatch.setattr(fetch, "_has_final", lambda rid: False)
    return fetch


def test_post_epoch_jst(monkeypatch):
    f = _mod(monkeypatch)
    # 2026-07-26 14:50 JST = 05:50 UTC
    import calendar
    assert f._post_epoch("20260726", "14:50") == calendar.timegm((2026, 7, 26, 5, 50, 0, 0, 0, 0))


def test_actionable_slot_is_nearest_due(monkeypatch):
    f = _mod(monkeypatch)
    assert f._actionable_slot(30.5, set()) == (45, 15)   # T-45 期限済み・T-30 は未来
    assert f._actionable_slot(29.9, set()) == (30, 10)   # T-30 の期限が来た
    assert f._actionable_slot(1.5, set()) == (2, 2)
    assert f._actionable_slot(250.0, set()) == (300, 60)  # ベースライン帯
    assert f._actionable_slot(500.0, set()) is None       # 最遠スロットより手前
    assert f._actionable_slot(29.9, {"T-30"}) is None     # 消化済みなら何もしない


def test_pick_prefers_most_overdue_ratio(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:00")  # 12:00 JST
    races = [
        # T-8 スロットを 1 分超過（比率 1/2 = 0.5）
        {"race_id": "20260726-mo-01", "post_time": "12:07", "source_key": "k1"},
        # T-45 スロットを 5 分超過（比率 5/15 = 0.33）
        {"race_id": "20260726-mo-02", "post_time": "12:40", "source_key": "k2"},
    ]
    race, slot, is_final = f._pick(now, races)
    assert race["race_id"] == "20260726-mo-01"
    assert slot == 8 and is_final is False


def test_pick_final_on_just_started(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:02")  # 12:00 発走の 2 分後
    races = [{"race_id": "20260726-mo-01", "post_time": "12:00", "source_key": "k"}]
    race, slot, is_final = f._pick(now, races)
    assert is_final is True and slot is None


def test_pick_none_when_slot_closed(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:00")
    races = [{"race_id": "20260726-mo-01", "post_time": "12:05",
              "source_key": "k", "closed_slots": ["T-6"]}]  # T-6 消化済み・T-4 は未来
    assert f._pick(now, races) is None


def test_near_beats_baseline(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:00")
    races = [
        # ベースライン: T-300 の期限を 50 分超過（比率 50/60）
        {"race_id": "20260726-ko-01", "post_time": "16:10", "source_key": "k1"},
        # 勝負どころ: T-15 の期限を 1 分超過（比率 1/5）
        {"race_id": "20260726-mo-01", "post_time": "12:14", "source_key": "k2"},
    ]
    race, slot, _ = f._pick(now, races)
    assert race["race_id"] == "20260726-mo-01"  # 比率が小さくても勝負どころ優先
    assert slot == 15


def test_baseline_fills_idle(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:00")
    races = [{"race_id": "20260726-ko-01", "post_time": "16:10", "source_key": "k"}]
    race, slot, _ = f._pick(now, races)
    assert race["race_id"] == "20260726-ko-01"
    assert slot == 300  # 250 分前 → T-300 スロット


def test_baseline_gated_before_8am(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "7:30")  # JST 7:30
    races = [{"race_id": "20260726-ko-01", "post_time": "14:00", "source_key": "k"}]
    assert f._pick(now, races) is None


def test_baseline_cooldown_after_failed_attempt(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:00")
    races = [{"race_id": "20260726-ko-01", "post_time": "16:10",
              "source_key": "k", "last_attempt": now - 600}]  # 10 分前に空振り
    assert f._pick(now, races) is None
    races[0]["last_attempt"] = now - 1900  # 30 分超過 → 再試行
    assert f._pick(now, races) is not None


def test_update_day_closes_due_slots(monkeypatch):
    from decimal import Decimal
    f = _mod(monkeypatch)
    written = []

    class FakeTable:
        def put_item(self, Item):
            written.append(Item)

    monkeypatch.setattr(f, "_TABLE", FakeTable())
    race = {"pk": "DAY#20260726", "sk": "RACE#x", "race_id": "x",
            "post_time": "12:00", "last_attempt": Decimal(1)}
    f._update_day_after_snapshot(race, {"odds": [2.0, 2.0], "horses": []}, 13.0)
    assert len(written) == 1
    closed = written[0]["closed_slots"]
    assert "T-15" in closed and "T-480" in closed  # 期限済みは全て閉じる
    assert "T-10" not in closed                     # 未来のスロットは開けたまま
    assert written[0]["top1"] == Decimal("0.5")
    assert "last_attempt" not in written[0]
