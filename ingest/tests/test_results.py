"""前日結果の回収（Issue #52。ネットワーク/DynamoDB 非依存）。"""
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.parse import parse_result  # noqa: E402

RESULT_HTML = """
<table class="dataTable">
  <tr><th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>人気</th></tr>
  <tr><td>1</td><td>1</td><td>5</td><td>アルファ</td><td>2</td></tr>
  <tr><td>2</td><td>3</td><td>1</td><td>ベータ</td><td>1</td></tr>
  <tr><td>中止</td><td>4</td><td>3</td><td>ガンマ</td><td>5</td></tr>
</table>"""


def test_parse_result_basic():
    finish = parse_result(RESULT_HTML)
    assert finish == [{"pos": 1, "num": 5}, {"pos": 2, "num": 1}]


def test_parse_result_none_on_unknown_structure():
    assert parse_result("<html><table><tr><td>x</td></tr></table></html>") is None


def _fetch(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import fetch
    importlib.reload(fetch)
    return fetch


def test_pick_result_prefers_yesterday_in_post_order(monkeypatch):
    f = _fetch(monkeypatch)
    now = f._post_epoch("20260728", "3:00")  # 朝の窓（JST 3:00）
    days = {
        "20260727": [
            {"race_id": "20260727-mo-02", "post_time": "12:20", "source_key": "k2"},
            {"race_id": "20260727-mo-01", "post_time": "11:45", "source_key": "k1"},
            {"race_id": "20260727-mo-03", "post_time": "12:55",
             "source_key": "k3", "result_ok": True},
        ],
        "20260726": [
            {"race_id": "20260726-mo-01", "post_time": "12:25", "source_key": "k0"},
        ],
    }
    monkeypatch.setattr(f, "_races_today", lambda d: days.get(d, []))
    race = f._pick_result(now)
    assert race["race_id"] == "20260727-mo-01"  # 前日を発走順に。回収済みは飛ばす


def test_pick_result_falls_back_to_two_days_ago(monkeypatch):
    f = _fetch(monkeypatch)
    now = f._post_epoch("20260728", "3:00")
    days = {
        "20260727": [{"race_id": "20260727-mo-01", "post_time": "11:45",
                      "source_key": "k", "result_ok": True}],
        "20260726": [{"race_id": "20260726-mo-01", "post_time": "12:25",
                      "source_key": "k0"}],
    }
    monkeypatch.setattr(f, "_races_today", lambda d: days.get(d, []))
    assert f._pick_result(now)["race_id"] == "20260726-mo-01"


def test_pick_result_closed_after_sales_open(monkeypatch):
    f = _fetch(monkeypatch)
    now = f._post_epoch("20260728", "10:30")  # 発売開始後は結果回収しない
    monkeypatch.setattr(
        f, "_races_today",
        lambda d: [{"race_id": "x", "post_time": "11:45", "source_key": "k"}])
    assert f._pick_result(now) is None


def test_pick_result_respects_cooldown(monkeypatch):
    f = _fetch(monkeypatch)
    now = f._post_epoch("20260728", "3:00")
    monkeypatch.setattr(
        f, "_races_today",
        lambda d: [{"race_id": "x", "post_time": "11:45", "source_key": "k",
                    "result_attempt": Decimal(int(now - 600))}] if d == "20260727" else [])
    assert f._pick_result(now) is None


def test_api_race_exposes_result_and_keeps_snapshots(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import api
    importlib.reload(api)

    def fake_query(pk, limit=None, desc=False):
        if pk.startswith("DAY#"):
            return [{
                "pk": "DAY#20260727", "sk": "RACE#20260727-mo-01",
                "race_id": "20260727-mo-01", "venue": "盛岡",
                "race_no": Decimal(1), "post_time": "11:45", "name": "x",
                "n_horses": Decimal(2), "surface": "ダ", "distance": Decimal(1200),
            }]
        return [
            {"pk": pk, "sk": "RESULT",
             "finish": [{"pos": Decimal(1), "num": Decimal(2)},
                        {"pos": Decimal(2), "num": Decimal(1)}]},
            {"pk": pk, "sk": "TS#11:00", "time": "11:00", "slot": "T-45",
             "horses": [{"num": Decimal(1), "name": "ア"},
                        {"num": Decimal(2), "name": "ベ"}],
             "odds": [Decimal("2.0"), Decimal("3.0")]},
        ]

    monkeypatch.setattr(api, "_query", fake_query)
    race = api._race("20260727-mo-01")
    assert race["result"] == [{"pos": 1, "num": 2}, {"pos": 2, "num": 1}]
    # RESULT 項目がスナップショット列や馬名の解決を汚さない
    assert race["horses"][0]["name"] == "ア"
    assert len(race["snapshots"]) == 1 and race["snapshots"][0]["slot"] == "T-45"


def test_index_survives_result_only_race(monkeypatch):
    """スナップショット 0 件 + RESULT ありのレースが index を壊さない（#63）。"""
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import api
    importlib.reload(api)

    def fake_query(pk, limit=None, desc=False, sk_prefix=None):
        if pk.startswith("DAY#"):
            return [{
                "pk": "DAY#20260726", "sk": "RACE#20260726-mo-01",
                "race_id": "20260726-mo-01", "venue": "盛岡",
                "race_no": Decimal(1), "post_time": "12:25", "name": "x",
                "n_horses": Decimal(2), "surface": "ダ", "distance": Decimal(1200),
            }]
        items = [{"pk": pk, "sk": "RESULT",
                  "finish": [{"pos": Decimal(1), "num": Decimal(1)}]}]
        if sk_prefix:
            items = [i for i in items if i["sk"].startswith(sk_prefix)]
        items.sort(key=lambda x: x["sk"], reverse=desc)
        return items[:limit] if limit else items

    monkeypatch.setattr(api, "_query", fake_query)
    idx = api._index("20260726")
    assert len(idx["races"]) == 1
    assert "top1" not in idx["races"][0]  # 指標なしで静かにスキップ


def test_pick_record_prefers_untaken_in_post_order(monkeypatch):
    f = _fetch(monkeypatch)
    now = f._post_epoch("20260728", "3:00")  # 朝の窓
    races = [
        {"race_id": "r2", "post_time": "12:20", "source_key": "k2"},
        {"race_id": "r1", "post_time": "11:45", "source_key": "k1"},
        {"race_id": "r3", "post_time": "12:55", "source_key": "k3", "record_ok": True},
    ]
    assert f._pick_record(now, races)["race_id"] == "r1"  # 発走順・取得済みは飛ばす


def test_pick_record_closed_after_sales_open(monkeypatch):
    f = _fetch(monkeypatch)
    now = f._post_epoch("20260728", "10:30")  # 発売開始後は馬柱を取らない
    races = [{"race_id": "r", "post_time": "11:45", "source_key": "k"}]
    assert f._pick_record(now, races) is None


def test_decimalize_converts_floats(monkeypatch):
    from decimal import Decimal
    f = _fetch(monkeypatch)
    out = f._decimalize({"a": 17.5, "b": [{"c": 3.5}], "d": 6, "e": "x"})
    assert out["a"] == Decimal("17.5")
    assert out["b"][0]["c"] == Decimal("3.5")
    assert out["d"] == 6 and out["e"] == "x"  # int/str は変えない
