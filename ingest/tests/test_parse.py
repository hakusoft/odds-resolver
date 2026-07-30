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


# 馬柱パーサ（#55）。合成 HTML で会場非依存・欠測堅牢性を検証する。
def _record_html(profile, recent_cells, extra_cols=1):
    """馬柱テーブルの最小 HTML。profile と近走セル列を差し込む。"""
    recent = "".join(f"<td>{c}</td>" for c in recent_cells)
    return f"""
    <table>
      <tr><th>枠番</th><th>馬番</th><th>父馬 馬名 母馬</th>
          <th>性齢 毛色 負担重量 騎手名 【勝率】 【3着内率】</th>
          <th>前5走 着順 頭数 競走日 距離</th><th>成績</th></tr>
      <tr><td>1</td><td>1</td><td>サイア<br>アルファ<br>ダム</td>
          <td>{profile}</td>{recent}<td>0 0 0</td></tr>
    </table>"""


def test_horse_records_banei():
    from ingest.parse import parse_horse_records
    html = _record_html(
        "牝3 鹿毛 570 長澤幸 （ばんえい） 【 17.5% 】 【 40.4% 】 小北栄",
        ["6 10頭 帯広 26.07.13 ビタS 200ダ 4人 鈴木恵 570 2:24.0"])
    recs = parse_horse_records(html, "帯広ば")
    assert len(recs) == 1
    r = recs[0]
    assert r["num"] == 1 and r["name"] == "アルファ"
    assert r["sex_age"] == "牝3" and r["weight_carried"] == 570
    assert r["jockey"] == "長澤幸"
    assert r["jockey_win_pct"] == 17.5 and r["jockey_top3_pct"] == 40.4
    assert r["recent"][0] == {"pos": 6, "field_size": 10, "date": "26.07.13",
                              "distance": 200, "surface": "ダ",
                              "popularity": 4, "time": "2:24.0"}


def test_horse_records_flat_venue():
    """会場非依存: 通常競馬（芝・55kg・4桁距離）でも同じパーサで抜ける。"""
    from ingest.parse import parse_horse_records
    html = _record_html(
        "牡5 芦毛 55 戸崎圭 （美浦） 【 25.0% 】 【 55.0% 】 藤沢和",
        ["3 12頭 大井 26.07.20 特別 1200ダ 2人 戸崎圭 55 1:12.3",
         "1 14頭 大井 26.07.06 条件 1400芝 1人 戸崎圭 55 1:24.5"])
    recs = parse_horse_records(html, "大井")
    r = recs[0]
    assert r["sex_age"] == "牡5" and r["weight_carried"] == 55
    assert r["jockey_win_pct"] == 25.0
    assert len(r["recent"]) == 2
    assert r["recent"][0]["distance"] == 1200 and r["recent"][0]["surface"] == "ダ"
    assert r["recent"][1]["surface"] == "芝"


def test_horse_records_missing_data():
    """欠測堅牢性: 勝率欠落・空の近走でも例外を出さず部分 dict を返す。"""
    from ingest.parse import parse_horse_records
    html = _record_html("牝4 栗毛 54 田中 （大井）", ["-", ""])
    recs = parse_horse_records(html, "大井")
    r = recs[0]
    assert "jockey_win_pct" not in r  # 【】欠落 → None（キー無し）
    assert r["recent"] == []           # 空の近走は落ちる


def test_horse_records_no_table():
    from ingest.parse import parse_horse_records
    assert parse_horse_records("<html><table><tr><td>x</td></tr></table></html>",
                               "大井") is None
