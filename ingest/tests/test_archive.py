"""夜間バッチ（ネットワーク/AWS 非依存。DynamoDB と S3 をスタブする）。"""
import io
import json
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

DAY_ITEMS = [
    {
        "pk": "DAY#20260726", "sk": "RACE#20260726-mo-01",
        "race_id": "20260726-mo-01", "venue": "盛岡", "race_no": Decimal(1),
        "post_time": "12:25", "name": "テスト１", "n_horses": Decimal(2),
        "surface": "ダ", "distance": Decimal(1200),
    },
    {
        "pk": "DAY#20260726", "sk": "RACE#20260726-mo-02",
        "race_id": "20260726-mo-02", "venue": "盛岡", "race_no": Decimal(2),
        "post_time": "13:00", "name": "テスト２", "n_horses": Decimal(2),
        "surface": "ダ", "distance": Decimal(1400),
    },
]

SNAPSHOTS = {
    "RACE#20260726-mo-01": [
        {
            "pk": "RACE#20260726-mo-01", "sk": "TS#12:00", "time": "12:00",
            "horses": [{"num": Decimal(1), "name": "アルファ"},
                       {"num": Decimal(2), "name": "ベータ"}],
            "odds": [Decimal("2.0"), Decimal("4.0")],
        },
        {
            "pk": "RACE#20260726-mo-01", "sk": "TS#12:20", "time": "12:20",
            "horses": [{"num": Decimal(1), "name": "アルファ"},
                       {"num": Decimal(2), "name": "ベータ"}],
            "odds": [Decimal("1.5"), Decimal("6.0")],
        },
        {
            "pk": "RACE#20260726-mo-01", "sk": "RESULT",
            "finish": [{"pos": Decimal(1), "num": Decimal(1)},
                       {"pos": Decimal(2), "num": Decimal(2)}],
        },
    ],
}


def _fake_query(pk, limit=None, desc=False, sk_prefix=None):
    if pk.startswith("DAY#"):
        items = [i for i in DAY_ITEMS if i["pk"] == pk]
    else:
        items = list(SNAPSHOTS.get(pk, []))
    if sk_prefix:
        items = [i for i in items if i["sk"].startswith(sk_prefix)]
    items.sort(key=lambda x: x["sk"], reverse=desc)
    return items[:limit] if limit else items


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        self.objects[(Bucket, Key)] = {"body": Body, "cc": CacheControl}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]["body"])}


def _setup(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    monkeypatch.setenv("DATA_BUCKET", "data-bkt")
    monkeypatch.setenv("FRONTEND_BUCKET", "front-bkt")
    import importlib
    from ingest import api, archive
    importlib.reload(api)
    importlib.reload(archive)
    monkeypatch.setattr(api, "_query", _fake_query)
    fake = FakeS3()
    monkeypatch.setattr(archive, "_s3", fake)
    return archive, fake


def _body(fake, bucket, key):
    return json.loads(fake.objects[(bucket, key)]["body"])


def test_run_writes_both_buckets_with_api_schema(monkeypatch):
    archive, fake = _setup(monkeypatch)
    result = archive.run("20260726")
    assert result == {"date": "20260726", "races": 2, "days": 1, "calib_races": 1}

    for bucket, prefix in (("data-bkt", ""), ("front-bkt", "data/")):
        idx = _body(fake, bucket, f"{prefix}20260726/index.json")
        assert idx["date"] == "2026-07-26"
        assert [r["race_id"] for r in idx["races"]] == \
            ["20260726-mo-01", "20260726-mo-02"]

        race = _body(fake, bucket, f"{prefix}races/20260726-mo-01.json")
        assert race["horses"] == [{"num": 1, "name": "アルファ"},
                                  {"num": 2, "name": "ベータ"}]
        assert [s["time"] for s in race["snapshots"]] == ["12:00", "12:20"]
        assert race["snapshots"][-1]["odds"] == [1.5, 6.0]


def test_index_metrics_from_latest_snapshot(monkeypatch):
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    races = _body(fake, "data-bkt", "20260726/index.json")["races"]
    # mo-01 は最新 TS#12:20 の支持率 (1/1.5)/(1/1.5+1/6.0) = 0.8
    assert abs(races[0]["top1"] - 0.8) < 1e-3
    assert "top1" not in races[1]  # スナップショット無しは指標なし


def test_days_json_dedupes_and_sorts_desc(monkeypatch):
    archive, fake = _setup(monkeypatch)
    fake.put_object(
        Bucket="data-bkt", Key="days.json",
        Body=json.dumps({"days": [
            {"date": "2026-07-27", "venues": ["高知"], "n_venues": 1, "n_races": 1},
            {"date": "2026-07-26", "venues": ["旧"], "n_venues": 1, "n_races": 99},
        ]}).encode(),
        ContentType="application/json", CacheControl="",
    )
    archive.run("20260726")
    days = _body(fake, "data-bkt", "days.json")["days"]
    assert [d["date"] for d in days] == ["2026-07-27", "2026-07-26"]
    assert days[1] == {"date": "2026-07-26", "venues": ["盛岡"],
                       "n_venues": 1, "n_races": 2}


def test_no_meeting_writes_nothing(monkeypatch):
    archive, fake = _setup(monkeypatch)
    result = archive.run("20260101")
    assert result["note"] == "no races"
    assert fake.objects == {}


def test_cache_control_policy(monkeypatch):
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    assert fake.objects[("data-bkt", "days.json")]["cc"] == "public, max-age=60"
    assert fake.objects[("data-bkt", "20260726/index.json")]["cc"] == \
        "public, max-age=3600"
    assert fake.objects[("data-bkt", "races/20260726-mo-01.json")]["cc"] == \
        "public, max-age=86400"


def test_handler_invalidates_only_past_day_rebakes(monkeypatch):
    import importlib
    from ingest import archive
    monkeypatch.setenv("TABLE_NAME", "dummy")
    importlib.reload(archive)

    calls = []
    monkeypatch.setattr(archive, "_invalidate", lambda d: calls.append(d) or f"ref-{d}")
    monkeypatch.setattr(archive, "run", lambda d=None: {"date": d or "TODAY", "races": 5})
    monkeypatch.setattr(archive, "jst_today", lambda: "20260728")

    # 過去日の再焼き → 無効化する
    out = archive.handler({"date": "20260726"}, None)
    assert out["invalidation"] == "ref-20260726" and calls == ["20260726"]

    # 当日の焼き（date 指定なし）→ 無効化しない
    calls.clear()
    archive.handler({}, None)
    assert calls == []

    # 当日を date 指定で焼いた場合も無効化しない
    archive.handler({"date": "20260728"}, None)
    assert calls == []


def test_handler_skips_invalidation_when_no_races(monkeypatch):
    import importlib
    from ingest import archive
    monkeypatch.setenv("TABLE_NAME", "dummy")
    importlib.reload(archive)

    calls = []
    monkeypatch.setattr(archive, "_invalidate", lambda d: calls.append(d))
    monkeypatch.setattr(archive, "run",
                        lambda d=None: {"date": d, "races": 0, "note": "no races"})
    monkeypatch.setattr(archive, "jst_today", lambda: "20260728")
    archive.handler({"date": "20260726"}, None)
    assert calls == []  # 非開催日は無効化も不要


# ---- 較正曲線（#53） ----

def test_calibration_bins_basic():
    from ingest.metrics import calibration_bins
    # 2.0/4.0/8.0/8.0 → 支持率 0.5/0.25/0.125/0.125、勝ち馬 index=0
    bins = calibration_bins([2.0, 4.0, 8.0, 8.0], 0)
    total_n = sum(b["n"] for b in bins)
    assert total_n == 4
    assert sum(b["wins"] for b in bins) == 1
    # 0.5 は最終ビン(0.5-1.0)に入り、そこに勝ち馬がいる
    assert bins[-1]["n"] == 1 and bins[-1]["wins"] == 1


def test_calibration_bins_none_on_dead_odds():
    from ingest.metrics import calibration_bins
    assert calibration_bins([None, None], None) is None


def test_accumulate_calib_counts_winner(monkeypatch):
    archive, _ = _setup(monkeypatch)
    acc = archive._empty_calib_set()
    race = {
        "horses": [{"num": 1}, {"num": 2}, {"num": 3}],
        "snapshots": [{"odds": [1.5, 5.0, 5.0]}],
        "result": [{"pos": 1, "num": 1}, {"pos": 2, "num": 2}],
    }
    assert archive._accumulate_calib(acc, race) is True
    assert sum(b["n"] for b in acc["total"]) == 3
    assert sum(b["wins"] for b in acc["total"]) == 1


def test_accumulate_calib_skips_no_result(monkeypatch):
    archive, _ = _setup(monkeypatch)
    acc = archive._empty_calib_set()
    race = {"horses": [{"num": 1}], "snapshots": [{"odds": [2.0]}], "result": []}
    assert archive._accumulate_calib(acc, race) is False
    assert sum(b["n"] for b in acc["total"]) == 0


def test_accumulate_calib_splits_by_surge(monkeypatch):
    archive, _ = _setup(monkeypatch)
    acc = archive._empty_calib_set()
    # 2番が T-8 で支持率を +8pt 以上 急上昇（10→3 でオッズ半減以上）して勝つ
    race = {
        "horses": [{"num": 1}, {"num": 2}, {"num": 3}],
        "snapshots": [
            {"slot": "T-45", "odds": [2.0, 10.0, 10.0]},
            {"slot": "T-8", "odds": [2.0, 3.0, 10.0]},
        ],
        "result": [{"pos": 1, "num": 2}, {"pos": 2, "num": 1}],
    }
    assert archive._accumulate_calib(acc, race) is True
    # 全馬 3、急変あり 1（2番）、急変なし 2（1・3番）
    assert sum(b["n"] for b in acc["total"]) == 3
    assert sum(b["n"] for b in acc["surged"]) == 1
    assert sum(b["n"] for b in acc["calm"]) == 2
    # 勝ったのは急変した 2番 → surged に wins が入り calm には入らない
    assert sum(b["wins"] for b in acc["surged"]) == 1
    assert sum(b["wins"] for b in acc["calm"]) == 0
    # 単勝回収は surged 側に勝ち馬のオッズ（3.0）が入る
    assert sum(b["payback"] for b in acc["surged"]) == 3.0


def test_calibration_json_written_and_dedupes_by_date(monkeypatch):
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    doc = _body(fake, "data-bkt", "calibration.json")
    assert doc["n_days"] == 1 and doc["n_races"] == 1
    first_n = sum(b["n"] for b in doc["total"])
    # 同じ日を再焼き → 二重計上せず総数が変わらない
    archive.run("20260726")
    doc2 = _body(fake, "data-bkt", "calibration.json")
    assert doc2["n_days"] == 1
    assert sum(b["n"] for b in doc2["total"]) == first_n
