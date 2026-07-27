"""指標の書き込み時前計算（Issue #48。ネットワーク/AWS 非依存）。"""
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.metrics import support_metrics  # noqa: E402


def test_support_metrics_basic():
    top1, ent = support_metrics([2.0, 4.0, 8.0, 8.0])
    assert abs(top1 - 0.5) < 1e-6
    assert 0 < ent < 1


def test_support_metrics_unavailable():
    assert support_metrics([None, None]) is None
    assert support_metrics([]) is None


def _api(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import api
    importlib.reload(api)
    return api


def test_index_uses_precomputed_without_race_query(monkeypatch):
    api = _api(monkeypatch)
    calls = []

    def fake_query(pk, limit=None, desc=False):
        calls.append(pk)
        return [{
            "pk": "DAY#20260726", "sk": "RACE#20260726-mo-01",
            "race_id": "20260726-mo-01", "venue": "盛岡", "race_no": Decimal(1),
            "post_time": "12:25", "name": "x", "n_horses": Decimal(8),
            "surface": "ダ", "distance": Decimal(1200),
            "top1": Decimal("0.5"), "ent": Decimal("0.81"),
        }]

    monkeypatch.setattr(api, "_query", fake_query)
    idx = api._index("20260726")
    assert idx["races"][0]["top1"] == 0.5
    assert idx["races"][0]["ent"] == 0.81
    assert calls == ["DAY#20260726"]  # RACE# への追加クエリが無い


def test_index_falls_back_when_not_precomputed(monkeypatch):
    api = _api(monkeypatch)
    calls = []

    def fake_query(pk, limit=None, desc=False):
        calls.append(pk)
        if pk.startswith("DAY#"):
            return [{
                "pk": "DAY#20260726", "sk": "RACE#20260726-mo-01",
                "race_id": "20260726-mo-01", "venue": "盛岡",
                "race_no": Decimal(1), "post_time": "12:25", "name": "x",
                "n_horses": Decimal(2), "surface": "ダ", "distance": Decimal(1200),
            }]
        return [{"odds": [Decimal("2.0"), Decimal("2.0")]}]

    monkeypatch.setattr(api, "_query", fake_query)
    idx = api._index("20260726")
    assert idx["races"][0]["top1"] == 0.5
    assert "RACE#20260726-mo-01" in calls


def test_fetch_closes_slots_even_when_metrics_unavailable(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import fetch
    importlib.reload(fetch)

    written = []

    class FakeTable:
        def put_item(self, Item):
            written.append(Item)

    monkeypatch.setattr(fetch, "_TABLE", FakeTable())
    race = {"pk": "DAY#20260726", "sk": "RACE#20260726-mo-01",
            "race_id": "20260726-mo-01", "post_time": "12:25"}
    fetch._update_day_after_snapshot(race, {"odds": [None, None], "horses": []}, 8.0)
    # 指標は付かないが、消化スロットの記録は行われる
    assert len(written) == 1
    assert "top1" not in written[0]
    assert "T-8" in written[0]["closed_slots"]


def test_race_normalizes_zero_odds_to_null(monkeypatch):
    api = _api(monkeypatch)

    def fake_query(pk, limit=None, desc=False):
        if pk.startswith("DAY#"):
            return [{
                "pk": "DAY#20260727", "sk": "RACE#20260727-mo-03",
                "race_id": "20260727-mo-03", "venue": "盛岡",
                "race_no": Decimal(3), "post_time": "12:55", "name": "x",
                "n_horses": Decimal(2), "surface": "ダ", "distance": Decimal(1200),
            }]
        return [{
            "pk": pk, "sk": "TS#10:23", "time": "10:23",
            "horses": [{"num": Decimal(1), "name": "ア"},
                       {"num": Decimal(2), "name": "ベ"}],
            "odds": [Decimal("0"), Decimal("2.5")],
        }]

    monkeypatch.setattr(api, "_query", fake_query)
    race = api._race("20260727-mo-03")
    # 格納済みの 0.0（過去データ）も読み出しで null へ正規化される
    assert race["snapshots"][0]["odds"] == [None, 2.5]
