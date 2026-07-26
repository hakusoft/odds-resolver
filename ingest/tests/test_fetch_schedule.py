"""フェッチャの段階制スケジューリング（ネットワーク/DynamoDB 非依存）。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


def _mod(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import fetch
    importlib.reload(fetch)
    return fetch


def test_post_epoch_jst(monkeypatch):
    f = _mod(monkeypatch)
    # 2026-07-26 14:50 JST = 05:50 UTC
    import calendar
    assert f._post_epoch("20260726", "14:50") == calendar.timegm((2026, 7, 26, 5, 50, 0, 0, 0, 0))


def test_desired_interval_stages(monkeypatch):
    f = _mod(monkeypatch)
    assert f._desired_interval_sec(40) == 15 * 60
    assert f._desired_interval_sec(15) == 5 * 60
    assert f._desired_interval_sec(5) == 2 * 60
    assert f._desired_interval_sec(60) is None    # 遠すぎ
    assert f._desired_interval_sec(-5) is None     # 発走後（確定は別処理）


def test_pick_prefers_imminent(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:00")  # 基準時刻
    # A: 発走5分後で確定未取得 → 最優先で final
    # B: 発走8分前・未取得
    races = [
        {"race_id": "20260726-mo-01", "post_time": "12:08", "source_key": "k1"},  # T-8
        {"race_id": "20260726-mo-02", "post_time": "12:40", "source_key": "k2"},  # T-40
    ]
    # 取得履歴なしをスタブ
    monkeypatch.setattr(f, "_last_snapshot_ts", lambda rid: None)
    monkeypatch.setattr(f, "_has_final", lambda rid: False)
    race, is_final = f._pick(now, races)
    assert race["race_id"] == "20260726-mo-01"  # T-8 が T-40 より切迫
    assert is_final is False


def test_pick_final_on_just_started(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:02")  # 12:00発走の2分後
    races = [{"race_id": "20260726-mo-01", "post_time": "12:00", "source_key": "k"}]
    monkeypatch.setattr(f, "_last_snapshot_ts", lambda rid: None)
    monkeypatch.setattr(f, "_has_final", lambda rid: False)
    race, is_final = f._pick(now, races)
    assert is_final is True  # 発走直後・確定未取得 → 確定を取る


def test_pick_none_when_all_recent(monkeypatch):
    f = _mod(monkeypatch)
    now = f._post_epoch("20260726", "12:00")
    races = [{"race_id": "20260726-mo-01", "post_time": "12:05", "source_key": "k"}]  # T-5, 2分毎
    monkeypatch.setattr(f, "_last_snapshot_ts", lambda rid: now - 30)  # 30秒前に取得済み
    monkeypatch.setattr(f, "_has_final", lambda rid: False)
    assert f._pick(now, races) is None  # 間隔内なので取らない
