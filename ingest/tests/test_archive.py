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
        "records": [{"num": Decimal(1), "name": "アルファ",
                     "jockey_win_pct": Decimal("17.5")}],
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

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        """本物と同じく 1 ページ最大 1000 件で切る（#69 の再集計が使う）。"""
        keys = sorted(k for (b, k) in self.objects if b == Bucket
                      and k.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start:start + 1000]
        out = {"Contents": [{"Key": k} for k in page]}
        if start + 1000 < len(keys):
            out["IsTruncated"] = True
            out["NextContinuationToken"] = str(start + 1000)
        return out


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


def test_accumulate_calib_splits_by_persistence_and_timing(monkeypatch):
    """急変を 持続/回帰（#88）と 直前/それ以前（#87）に割って積む。

    2 軸は独立なので、同じ 1 頭が persist と early に同時に入りうる。
    """
    archive, _ = _setup(monkeypatch)
    acc = archive._empty_calib_set()

    def field(second, n=8, base=8.0):
        o = [base] * n
        o[1] = second
        return o

    # 2番が T-15（5分より前）に急変し、そのまま持続して勝つ
    race = {
        "horses": [{"num": i + 1} for i in range(8)],
        "snapshots": [
            {"slot": "T-20", "odds": field(8.0)},
            {"slot": "T-15", "odds": field(2.0)},
            {"slot": "F", "odds": field(2.0)},
        ],
        "result": [{"pos": 1, "num": 2}],
    }
    assert archive._accumulate_calib(acc, race) is True
    assert sum(b["n"] for b in acc["surged"]) == 1
    # 持続 = 1 / 回帰 = 0
    assert sum(b["n"] for b in acc["persist"]) == 1
    assert sum(b["n"] for b in acc["revert"]) == 0
    # 直前 = 0 / それ以前 = 1（T-15 は 5 分より前）
    assert sum(b["n"] for b in acc["late"]) == 0
    assert sum(b["n"] for b in acc["early"]) == 1
    # 勝ちと回収は該当する両方の系統に入る（軸が独立なので二重ではない）
    assert sum(b["wins"] for b in acc["persist"]) == 1
    assert sum(b["wins"] for b in acc["early"]) == 1


def test_calibration_doc_exposes_new_axes(monkeypatch):
    """calibration.json に by_persistence / by_timing が出る（後方互換）。"""
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    doc = _body(fake, "data-bkt", "calibration.json")
    # 既存キーは残っている
    assert "total" in doc and "by_surge" in doc
    # 新キーが増えている
    assert set(doc["by_persistence"]) == {"persist", "revert", "since"}
    assert set(doc["by_timing"]) == {"late", "early", "since"}
    # 分母の期間が total とズレるので、開始日を明示する
    assert doc["by_persistence"]["since"] == "20260726"
    assert doc["by_timing"]["since"] == "20260726"
    # 閾値の記録に窓が載る（後から定義を追える）
    assert doc["surge_threshold"]["late_window"] == 5


def test_since_is_none_when_only_legacy_days(monkeypatch):
    """新軸を持たない日しかなければ since は None（0 を実績と誤読させない）。"""
    archive, fake = _setup(monkeypatch)
    doc = {"by_date": {"20260701": {"races": 10, "sets": {
        "total": archive._empty_bins(), "surged": archive._empty_bins(),
        "calm": archive._empty_bins()}}}}
    monkeypatch.setattr(archive, "_load_calibration", lambda: doc)
    archive._update_calibration("20260701", doc["by_date"]["20260701"]["sets"], 10)
    out = _body(fake, "data-bkt", "calibration.json")
    assert out["by_persistence"]["since"] is None


def test_place_bins_uses_win_support_and_top3(monkeypatch):
    """複勝は単勝支持率でビンを切り、3着以内を hits に数える（#89）。"""
    from ingest.metrics import place_bins
    # 単勝 2.0/4.0/8.0/8.0 → 支持率 0.5/0.25/0.125/0.125
    odds = [2.0, 4.0, 8.0, 8.0]
    place = [{"lo": 1.1, "hi": 1.4}, {"lo": 1.6, "hi": 2.2},
             {"lo": 3.0, "hi": 5.0}, {"lo": 3.2, "hi": 5.4}]
    bins = place_bins(odds, place, top3_idx={0, 1, 2})
    assert sum(b["n"] for b in bins) == 4
    assert sum(b["hits"] for b in bins) == 3
    # 回収は下限で積む（安全側）。1.1 + 1.6 + 3.0
    assert abs(sum(b["payback"] for b in bins) - 5.7) < 1e-9
    # 支持率 0.5 の馬は最終ビンに入り、3着内なので hits も立つ
    assert bins[-1]["n"] == 1 and bins[-1]["hits"] == 1


def test_place_bins_skips_horses_without_place(monkeypatch):
    """複勝が取れていない馬は分母にも入れない。"""
    from ingest.metrics import place_bins
    odds = [2.0, 4.0, 8.0, 8.0]
    place = [{"lo": 1.1, "hi": 1.4}, None, None, {"lo": 3.2, "hi": 5.4}]
    bins = place_bins(odds, place, top3_idx={0, 1})
    assert sum(b["n"] for b in bins) == 2       # None の 2 頭は除外
    assert sum(b["hits"] for b in bins) == 1    # 3着内は 1番のみ（2番は除外済み）


def test_accumulate_calib_adds_place_when_present(monkeypatch):
    archive, _ = _setup(monkeypatch)
    acc = archive._empty_calib_set()
    race = {
        "horses": [{"num": 1}, {"num": 2}, {"num": 3}, {"num": 4}],
        "snapshots": [
            {"slot": "T-45", "odds": [2.0, 4.0, 8.0, 8.0],
             "place": [{"lo": 1.1, "hi": 1.4}, {"lo": 1.6, "hi": 2.2},
                       {"lo": 3.0, "hi": 5.0}, {"lo": 3.2, "hi": 5.4}]},
        ],
        "result": [{"pos": 1, "num": 1}, {"pos": 2, "num": 2}, {"pos": 3, "num": 3}],
    }
    assert archive._accumulate_calib(acc, race) is True
    assert sum(b["n"] for b in acc["place"]["total"]) == 4
    assert sum(b["hits"] for b in acc["place"]["total"]) == 3
    # 単勝側も従来どおり積まれている（1着は1番のみ）
    assert sum(b["wins"] for b in acc["total"]) == 1


def test_accumulate_calib_without_place_is_noop(monkeypatch):
    """複勝を取り始める前のレースでも壊れず、place は空のまま。"""
    archive, _ = _setup(monkeypatch)
    acc = archive._empty_calib_set()
    race = {
        "horses": [{"num": 1}, {"num": 2}],
        "snapshots": [{"slot": "T-45", "odds": [2.0, 4.0]}],
        "result": [{"pos": 1, "num": 1}],
    }
    assert archive._accumulate_calib(acc, race) is True
    assert sum(b["n"] for b in acc["place"]["total"]) == 0
    assert sum(b["n"] for b in acc["total"]) == 2


def test_calibration_doc_omits_place_before_any_data(monkeypatch):
    """複勝が1日も無ければ place キー自体を出さない（0 を実績と誤読させない）。"""
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    doc = _body(fake, "data-bkt", "calibration.json")
    assert "place" not in doc


def test_recalc_rebuilds_calibration_from_s3(monkeypatch):
    """TTL 切れでも S3 の races/*.json から較正を積み直せる（#69）。"""
    archive, fake = _setup(monkeypatch)
    # 先に通常の焼きで races/*.json を用意する
    archive.run("20260726")
    before = _body(fake, "data-bkt", "calibration.json")
    n_before = sum(b["n"] for b in before["total"])
    assert n_before > 0

    # 較正だけ消して DynamoDB も空にする（TTL 切れの再現）
    del fake.objects[("data-bkt", "calibration.json")]
    monkeypatch.setattr(archive, "_index",
                        lambda d: (_ for _ in ()).throw(KeyError(d)))
    assert archive.run("20260726")["races"] == 0      # 通常経路は復元不能

    out = archive.recalc("20260726")                  # S3 起点なら復元できる
    assert out["races"] > 0 and out["source"] == "s3"
    after = _body(fake, "data-bkt", "calibration.json")
    assert sum(b["n"] for b in after["total"]) == n_before


def test_recalc_reports_when_no_archived_races(monkeypatch):
    archive, _ = _setup(monkeypatch)
    out = archive.recalc("20991231")
    assert out["races"] == 0 and "no archived" in out["note"]


def test_recalc_does_not_rewrite_race_views(monkeypatch):
    """再集計は読むだけ。races/*.json と index を書き換えない。"""
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    race_keys = {k: v["body"] for (b, k), v in fake.objects.items()
                 if k.startswith("races/")}
    index_before = _body(fake, "data-bkt", "20260726/index.json")

    archive.recalc("20260726")

    after = {k: v["body"] for (b, k), v in fake.objects.items()
             if k.startswith("races/")}
    assert after == race_keys
    assert _body(fake, "data-bkt", "20260726/index.json") == index_before


def test_handler_routes_recalc_mode(monkeypatch):
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    seen = {}

    def _fake_recalc(d):
        seen["date"] = d
        return {"ok": True}

    monkeypatch.setattr(archive, "recalc", _fake_recalc)
    assert archive.handler({"mode": "recalc", "date": "20260726"}, None) == {"ok": True}
    assert seen["date"] == "20260726"


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


def test_archive_bakes_horse_records_to_s3(monkeypatch):
    """馬柱（#55）が race JSON に一緒に焼かれる。api の整形を共用するため。"""
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    for bucket, prefix in (("data-bkt", ""), ("front-bkt", "data/")):
        race = _body(fake, bucket, f"{prefix}races/20260726-mo-01.json")
        assert race["records"][0]["num"] == 1
        assert race["records"][0]["jockey_win_pct"] == 17.5  # Decimal→float


# ---- 当日総括レポート（#83） ----

def test_classify_race_firm():
    from ingest.metrics import classify_race
    # 1番人気(num1,支持率高)が1着 = 堅い
    race = {
        "horses": [{"num": 1, "name": "ア"}, {"num": 2, "name": "イ"}],
        "snapshots": [{"odds": [1.5, 6.0]}],
        "result": [{"pos": 1, "num": 1}, {"pos": 2, "num": 2}],
    }
    c = classify_race(race)
    assert c["firm"] is True and c["upset"] is False


def test_classify_race_upset():
    from ingest.metrics import classify_race
    # 支持率の低い num3 が1着・1番人気 num1 は着外 = 純粋な波乱
    race = {
        "horses": [{"num": 1}, {"num": 2}, {"num": 3}, {"num": 4}],
        "snapshots": [{"odds": [1.3, 5.0, 50.0, 8.0]}],  # num3 の支持率 <10%
        "result": [{"pos": 1, "num": 3}, {"pos": 2, "num": 2},
                   {"pos": 3, "num": 4}, {"pos": 4, "num": 1}],
    }
    c = classify_race(race)
    assert c["upset"] is True and c["firm"] is False  # 1番人気 num1 は4着


def test_classify_race_none_without_result():
    from ingest.metrics import classify_race
    assert classify_race({"snapshots": [{"odds": [2.0]}]}) is None


def test_summary_in_index(monkeypatch):
    archive, fake = _setup(monkeypatch)
    archive.run("20260726")
    for bucket, prefix in (("data-bkt", ""), ("front-bkt", "data/")):
        idx = _body(fake, bucket, f"{prefix}20260726/index.json")
        s = idx["summary"]
        # mo-01 は着順あり(1番人気1着=堅い)、mo-02 は着順なし
        assert s["n_races"] == 1 and s["firm"] == 1
        assert "hardest" in s
