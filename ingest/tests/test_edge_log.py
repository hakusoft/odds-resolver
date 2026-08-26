"""二軸の乖離ログ（#117 Phase 2-3）。ネットワーク/AWS 非依存。"""
import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.tests.test_archive import FakeS3  # noqa: E402


def _setup(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    monkeypatch.setenv("DATA_BUCKET", "data-bkt")
    monkeypatch.setenv("FRONTEND_BUCKET", "front-bkt")
    import importlib
    from ingest import archive
    importlib.reload(archive)
    fake = FakeS3()
    monkeypatch.setattr(archive, "_s3", fake)
    return archive, fake


RACE = {
    "race_id": "20260826-oi-01", "venue": "大井",
    "result": [{"num": 3, "pos": 1}, {"num": 5, "pos": 2}],
    "edges": [
        {"num": 3, "name": "アルファ", "p_form": 0.25, "p_market": 0.05,
         "odds": 12.4, "edge": 1.61, "form_score": 0.8, "slot_minutes": 8,
         "signaled_at": 1786516034},
        {"num": 9, "name": "ゾーン", "p_form": 0.20, "p_market": 0.04,
         "edge": 1.61, "form_score": 0.7, "slot_minutes": 8,
         "signaled_at": 1786516034},
    ],
}


def test_edge_log_attaches_result(monkeypatch):
    archive, fake = _setup(monkeypatch)
    n = archive._append_edge_log("20260826", [RACE])
    assert n == 2
    body = json.loads(fake.objects[("data-bkt", "edge/20260826.json")]["body"])
    by = {r["num"]: r for r in body["rows"]}
    # 勝った馬
    assert by[3]["pos"] == 1 and by[3]["won"] is True and by[3]["top3"] is True
    # 着順が付いていない馬（3着以下で result に載らない）は None
    assert by[9]["pos"] is None and by[9]["won"] is False


def test_edge_log_keeps_prediction_fields(monkeypatch):
    """判定時点の値がそのまま残る。後から書き換えないことが検証の担保。"""
    archive, fake = _setup(monkeypatch)
    archive._append_edge_log("20260826", [RACE])
    body = json.loads(fake.objects[("data-bkt", "edge/20260826.json")]["body"])
    r = body["rows"][0]
    assert r["p_form"] == 0.25 and r["p_market"] == 0.05
    assert r["edge"] == 1.61 and r["slot_minutes"] == 8
    # 回収率の計算に要る。p_market からは復元できない（#117 Phase 3）
    assert r["odds"] == 12.4


def test_edge_log_is_write_once(monkeypatch):
    """既にあれば書かない。上書きを許すと『後から書き換えていない』が崩れる。"""
    archive, fake = _setup(monkeypatch)
    archive._append_edge_log("20260826", [RACE])
    before = dict(fake.objects[("data-bkt", "edge/20260826.json")])
    changed = {**RACE, "edges": [{**RACE["edges"][0], "edge": 99.9}]}
    assert archive._append_edge_log("20260826", [changed]) == 0
    assert fake.objects[("data-bkt", "edge/20260826.json")] == before


def test_edge_log_skips_races_without_edges(monkeypatch):
    archive, fake = _setup(monkeypatch)
    assert archive._append_edge_log("20260826", [{"race_id": "x", "result": []}]) == 0
    assert ("data-bkt", "edge/20260826.json") not in fake.objects


def test_edge_log_writes_both_buckets(monkeypatch):
    archive, fake = _setup(monkeypatch)
    archive._append_edge_log("20260826", [RACE])
    assert ("data-bkt", "edge/20260826.json") in fake.objects
    assert ("front-bkt", "data/edge/20260826.json") in fake.objects
