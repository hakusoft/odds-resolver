"""汚染 jockey の欠測化（#118）。ネットワーク/AWS 非依存。"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.parse import is_contaminated_jockey  # noqa: E402
from ingest.tests.test_archive import FakeS3  # noqa: E402


@pytest.mark.parametrize("value", ["56.0", "54.0", "57", " 56.0 ", "0", "10"])
def test_numeric_jockey_is_contaminated(value):
    assert is_contaminated_jockey(value) is True


@pytest.mark.parametrize("value", [
    "武豊", "▲南部楓", "☆吉田理", "◇佐々世", "★塩津璃",  # 減量記号つきも実在
    None,          # 欠測は汚染ではない
    "",            # 空も汚染ではない（数値ではない）
    "56.0kg",      # 単位つきは数値のみではない
    "A1",
])
def test_legit_or_missing_jockey_is_not_contaminated(value):
    assert is_contaminated_jockey(value) is False


def test_weight_carried_mismatch_still_detected():
    """斤量側も壊れている 16 件（斤量 10〜13）を取りこぼさない。

    判定に weight_carried との一致を使うと、この形が漏れる。
    """
    from ingest.tools.fix_jockey import scrub
    race = {"records": [{"num": 3, "jockey": "54.0", "weight_carried": 10}]}
    assert scrub(race) == 1
    assert race["records"][0]["jockey"] is None


def test_scrub_sets_none_and_keeps_other_fields():
    from ingest.tools.fix_jockey import scrub
    race = {"records": [
        {"num": 1, "jockey": "56.0", "weight_carried": 56, "name": "アルファ"},
        {"num": 2, "jockey": "武豊", "weight_carried": 54, "name": "ベータ"},
        {"num": 3, "name": "ガンマ"},  # jockey キーごと無い
    ]}
    assert scrub(race) == 1
    recs = race["records"]
    # キーを消さず None を入れる（「取得したが無い」と「項目が無い」の区別を残す）
    assert "jockey" in recs[0] and recs[0]["jockey"] is None
    assert recs[0]["weight_carried"] == 56  # 壊れた斤量には触らない
    assert recs[0]["name"] == "アルファ"
    assert recs[1]["jockey"] == "武豊"       # 正常な騎手名は残す
    assert "jockey" not in recs[2]           # 無いものは足さない


def test_scrub_is_idempotent():
    from ingest.tools.fix_jockey import scrub
    race = {"records": [{"num": 1, "jockey": "56.0"}]}
    assert scrub(race) == 1
    assert scrub(race) == 0  # 2 回目は何も落ちない


def _setup(monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", "data-bkt")
    monkeypatch.setenv("FRONTEND_BUCKET", "front-bkt")
    import importlib
    from ingest.tools import fix_jockey
    importlib.reload(fix_jockey)
    fake = FakeS3()
    monkeypatch.setattr(fix_jockey, "_s3", fake)
    return fix_jockey, fake


def _put(fake, key, race):
    body = json.dumps(race, ensure_ascii=False).encode("utf-8")
    fake.objects[("data-bkt", key)] = {"body": body, "cc": ""}
    fake.objects[("front-bkt", "data/" + key)] = {"body": body, "cc": ""}


def _get(fake, bucket, key):
    return json.loads(fake.objects[(bucket, key)]["body"])


DIRTY = {"records": [{"num": 1, "jockey": "56.0"}, {"num": 2, "jockey": "武豊"}]}
CLEAN = {"records": [{"num": 1, "jockey": "岩田康"}]}


def test_dry_run_reports_without_writing(monkeypatch):
    fix, fake = _setup(monkeypatch)
    _put(fake, "races/20260731-kw-01.json", DIRTY)
    res = fix.run(apply=False)
    assert res == {"scanned": 1, "races_fixed": 1, "horses_fixed": 1,
                   "applied": False}
    # 書き込んでいない
    assert _get(fake, "data-bkt", "races/20260731-kw-01.json") == DIRTY


def test_apply_writes_both_buckets(monkeypatch):
    fix, fake = _setup(monkeypatch)
    _put(fake, "races/20260731-kw-01.json", DIRTY)
    res = fix.run(apply=True)
    assert res["horses_fixed"] == 1

    for bucket, key in (("data-bkt", "races/20260731-kw-01.json"),
                        ("front-bkt", "data/races/20260731-kw-01.json")):
        recs = _get(fake, bucket, key)["records"]
        assert recs[0]["jockey"] is None
        assert recs[1]["jockey"] == "武豊"


def test_cache_control_matches_archive(monkeypatch):
    """書き戻しの Cache-Control は archive が焼くのと同じ値にする。

    ここだけ違うと、直したファイルのキャッシュ挙動が他とズレる。
    """
    from ingest.archive import _CC_RACE
    fix, fake = _setup(monkeypatch)
    _put(fake, "races/20260731-kw-01.json", DIRTY)
    fix.run(apply=True)
    for bucket, key in (("data-bkt", "races/20260731-kw-01.json"),
                        ("front-bkt", "data/races/20260731-kw-01.json")):
        assert fake.objects[(bucket, key)]["cc"] == _CC_RACE


def test_clean_race_is_not_rewritten(monkeypatch):
    """汚染が無いファイルは put しない（無駄な書き込みと invalidation を避ける）。"""
    fix, fake = _setup(monkeypatch)
    _put(fake, "races/20260807-oi-01.json", CLEAN)
    before = dict(fake.objects[("data-bkt", "races/20260807-oi-01.json")])
    res = fix.run(apply=True)
    assert res == {"scanned": 1, "races_fixed": 0, "horses_fixed": 0,
                   "applied": True}
    assert fake.objects[("data-bkt", "races/20260807-oi-01.json")] == before


def test_date_filter_limits_scope(monkeypatch):
    fix, fake = _setup(monkeypatch)
    _put(fake, "races/20260731-kw-01.json", DIRTY)
    _put(fake, "races/20260801-oi-01.json", DIRTY)
    res = fix.run(date="20260731", apply=True)
    assert res["scanned"] == 1 and res["horses_fixed"] == 1
    # 対象外の日は触られていない
    assert _get(fake, "data-bkt", "races/20260801-oi-01.json") == DIRTY


def test_verify_counts_both_buckets(monkeypatch):
    fix, fake = _setup(monkeypatch)
    _put(fake, "races/20260731-kw-01.json", DIRTY)
    before = fix.verify()
    assert before["data"] == {"files": 1, "contaminated": 1}
    assert before["frontend"] == {"files": 1, "contaminated": 1}

    fix.run(apply=True)
    after = fix.verify()
    assert after["data"] == {"files": 1, "contaminated": 0}
    assert after["frontend"] == {"files": 1, "contaminated": 0}


def test_parse_guard_drops_numeric_jockey():
    """パース側でも数値 jockey を落とす（再発時に汚染を作らない）。"""
    from ingest.parse import _parse_profile

    class _Cell:
        def __init__(self, txt):
            self._t = txt

        def get_text(self, sep, strip):
            return self._t

    # 毛色と騎手が並ぶ正常形
    rec = {}
    _parse_profile(_Cell("牡3 鹿毛 54.0 武豊 【調教師】 17.5% 40.0%"), rec)
    assert rec.get("jockey") == "武豊"

    # 騎手位置に数値しか来ない形（#112 の再現）は jockey を立てない
    rec = {}
    _parse_profile(_Cell("牡3 鹿毛 54.0 56.0 【調教師】 17.5% 40.0%"), rec)
    assert "jockey" not in rec
