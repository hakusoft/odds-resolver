"""パーサのテスト（ネットワーク非依存）。

fixtures/ は取得元の実 HTML で、再配布を避けるため gitignore されている。
手元に置いた場合のみ実行し、無ければ skip する。
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.parse import parse_day_list, parse_race_list  # noqa: E402
from ingest.race_id import make_race_id  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"


def _read(name):
    p = FIX / name
    if not p.exists():
        pytest.skip(f"fixture がありません: {name}（tests/fixtures/ は gitignore）")
    return p.read_text(encoding="utf-8")


def test_day_list_resolves_venues_by_code():
    venues = parse_day_list(_read("day_list.html"), "20260724")
    assert {v["venue"] for v in venues} == {"大井", "笠松", "園田"}
    assert all("ナイター" not in v["venue"] for v in venues)


def test_race_list_is_complete():
    races = parse_race_list(_read("race_list_oi.html"), "大井")
    assert len(races) == 12
    for r in races:
        assert r["post_time"]
        assert r["n_horses"] > 0
        assert r["distance"] > 0
        assert r["surface"] in ("芝", "ダ")
        assert r["name"]


def test_race_id_format():
    races = parse_race_list(_read("race_list_sd.html"), "園田")
    assert make_race_id("20260724", "園田", races[0]["race_no"]) == "20260724-sd-01"


def test_day_list_empty_on_wrong_date():
    assert parse_day_list(_read("day_list.html"), "20990101") == []


def test_parse_odds_zero_becomes_none():
    # 発売直後の未投票馬は「0.0」と表示される。fixture 不要の合成 HTML で検証
    from ingest.parse import parse_odds
    html = """
    <table class="dataTable">
      <tr><th>馬番</th><th>馬名</th><th>単勝オッズ</th></tr>
      <tr><td>1</td><td>アルファ</td><td>0.0</td></tr>
      <tr><td>2</td><td>ベータ</td><td>2.5</td></tr>
      <tr><td>3</td><td>ガンマ</td><td>取消</td></tr>
    </table>"""
    p = parse_odds(html)
    assert [h["num"] for h in p["horses"]] == [1, 2, 3]
    assert p["odds"] == [None, 2.5, None]
