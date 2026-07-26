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
    ],
}


def _fake_query(pk, limit=None, desc=False):
    if pk.startswith("DAY#"):
        items = [i for i in DAY_ITEMS if i["pk"] == pk]
    else:
        items = list(SNAPSHOTS.get(pk, []))
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
    assert result == {"date": "20260726", "races": 2, "days": 1}

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
