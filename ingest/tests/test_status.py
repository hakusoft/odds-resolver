"""毎日の様子見サマリ（status.json）。ネットワーク/AWS 非依存。

**率を載せない**ことがこのファイルの肝。検証中に勝率・回収率が目に入ると
良い日・悪い日で判断が揺れる（#106 がそれを避けるために基準を先に置いた）。
"""
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


def _body(fake, key="status.json"):
    return json.loads(fake.objects[("data-bkt", key)]["body"])


RACES = [
    {"race_id": "20260826-oi-01", "venue": "大井",
     "records": [{"num": 1}], "result": [{"pos": 1, "num": 1}],
     "exotic": {"umatan": {"1-2": 12.3, "2-1": 45.6}},
     "edges": [{"num": 1, "name": "アルファ", "edge": 3.5,
                "p_form": 0.25, "p_market": 0.05, "odds": 18.0}]},
    {"race_id": "20260826-oi-02", "venue": "大井",
     "records": [{"num": 1}]},
]


def test_status_reports_coverage(monkeypatch):
    archive, fake = _setup(monkeypatch)
    archive._update_status("20260826", {"races": RACES}, RACES, [{}, {}])
    s = _body(fake)
    assert s["date"] == "20260826"
    assert s["days"] == 2 and s["races_today"] == 2
    cov = s["coverage"]
    assert cov["records"] == 2 and cov["results"] == 1
    assert cov["exotic_races"] == 1 and cov["exotic_pairs"] == 2


def test_status_never_exposes_rates(monkeypatch):
    """勝率・回収率を載せない。検証中に見ると判断が揺れる（#106）。

    このテストが落ちたら、その変更は検証の規律を壊している。
    """
    archive, fake = _setup(monkeypatch)
    archive._update_status("20260826", {"races": RACES}, RACES, [{}])
    blob = json.dumps(_body(fake), ensure_ascii=False)
    for banned in ("win_rate", "payback", "won", "top3", "pos", "hits"):
        assert banned not in blob, f"{banned} が漏れている"


def test_status_picks_omit_results(monkeypatch):
    """候補馬は記録の表示に留める。着順を混ぜない。"""
    archive, fake = _setup(monkeypatch)
    archive._update_status("20260826", {"races": RACES}, RACES, [{}])
    p = _body(fake)["picks"][0]
    assert set(p) == {"race_id", "venue", "num", "name",
                      "edge", "p_form", "p_market"}


def test_status_accumulates_n_across_days(monkeypatch):
    """n は前日ぶんに足す。S3 の list を毎回舐めない。"""
    archive, fake = _setup(monkeypatch)
    archive._update_status("20260825", {"races": RACES}, RACES, [{}])
    assert _body(fake)["edge"]["n"] == 1
    archive._update_status("20260826", {"races": RACES}, RACES, [{}])
    assert _body(fake)["edge"]["n"] == 2


def test_status_estimates_eta(monkeypatch):
    archive, fake = _setup(monkeypatch)
    archive._update_status("20260826", {"races": RACES}, RACES, [{}])
    e = _body(fake)["edge"]
    assert e["target"] == 300
    assert e["remaining"] == 299
    assert e["eta_days"] == 299        # 1 件/日ペース


def test_status_handles_no_picks(monkeypatch):
    """閾値超えが無い日もある。0 件で壊れない。"""
    archive, fake = _setup(monkeypatch)
    plain = [{"race_id": "x", "venue": "大井"}]
    archive._update_status("20260826", {"races": plain}, plain, [{}])
    s = _body(fake)
    assert s["picks"] == []
    assert s["edge"]["eta_days"] is None   # ペース 0 なら見込みを出さない


def test_status_target_matches_judge(monkeypatch):
    """判定基準と食い違わせない。片方だけ変えると表示が嘘になる。"""
    archive, _ = _setup(monkeypatch)
    from ingest.tools.judge_edge import MIN_N
    assert archive._EDGE_TARGET_N == MIN_N
