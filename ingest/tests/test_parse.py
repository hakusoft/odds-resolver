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
    # 複勝列が無い表でも壊れない（全て None で埋める）
    assert p["place"] == [None, None, None]


def test_parse_odds_reads_place_range():
    """複勝は範囲（lo-hi）で載る。実ページの列構成に合わせた合成 HTML（#89）。"""
    from ingest.parse import parse_odds
    html = """
    <table class="dataTable">
      <tr><th>枠番</th><th>馬番</th><th>馬名</th>
          <th>単勝オッズ</th><th>複勝オッズ</th><th>人気</th></tr>
      <tr><td>1</td><td>1</td><td>アルファ</td>
          <td>9.4</td><td>2.2-4.4</td><td>4番人気</td></tr>
      <tr><td>2</td><td>2</td><td>ベータ</td>
          <td>2.4</td><td>1.2 - 1.7</td><td>1番人気</td></tr>
      <tr><td>3</td><td>3</td><td>ガンマ</td>
          <td>0.0</td><td>0.0-0.0</td><td>-</td></tr>
    </table>"""
    p = parse_odds(html)
    assert p["odds"] == [9.4, 2.4, None]
    # 空白入りの範囲も読む。発売前の 0.0-0.0 は「まだ無い」= None
    assert p["place"] == [{"lo": 2.2, "hi": 4.4}, {"lo": 1.2, "hi": 1.7}, None]


def test_parse_odds_place_sorted_by_num():
    """複勝も馬番順に並べ替わる（odds と添字が一致する）。"""
    from ingest.parse import parse_odds
    html = """
    <table class="dataTable">
      <tr><th>馬番</th><th>馬名</th><th>単勝オッズ</th><th>複勝オッズ</th></tr>
      <tr><td>3</td><td>ガンマ</td><td>5.0</td><td>1.5-2.0</td></tr>
      <tr><td>1</td><td>アルファ</td><td>2.0</td><td>1.1-1.3</td></tr>
    </table>"""
    p = parse_odds(html)
    assert [h["num"] for h in p["horses"]] == [1, 3]
    assert p["odds"] == [2.0, 5.0]
    assert p["place"] == [{"lo": 1.1, "hi": 1.3}, {"lo": 1.5, "hi": 2.0}]


def test_parse_odds_place_malformed_is_none():
    """取消・想定外の表記は None（例外を出さない）。"""
    from ingest.parse import parse_odds
    html = """
    <table class="dataTable">
      <tr><th>馬番</th><th>馬名</th><th>単勝オッズ</th><th>複勝オッズ</th></tr>
      <tr><td>1</td><td>アルファ</td><td>2.0</td><td>取消</td></tr>
      <tr><td>2</td><td>ベータ</td><td>3.0</td><td></td></tr>
    </table>"""
    p = parse_odds(html)
    assert p["place"] == [None, None]


# 馬柱パーサ（#55, #114）。合成 HTML で会場非依存・欠測堅牢性を検証する。
# 馬番セルには実サイトと同じく rowspan を付ける（#114: rowspan の無い
# 行は「持ち時計」等の中間行としてスキップされるため、無いと 0 件になる）。
def _record_html(profile, recent_cells, extra_cols=1):
    """馬柱テーブルの最小 HTML。profile と近走セル列を差し込む。"""
    recent = "".join(f"<td>{c}</td>" for c in recent_cells)
    return f"""
    <table>
      <tr><th>枠番</th><th>馬番</th><th>父馬 馬名 母馬</th>
          <th>性齢 毛色 負担重量 騎手名 【勝率】 【3着内率】</th>
          <th>前5走 着順 頭数 競走日 距離</th><th>成績</th></tr>
      <tr><td>1</td><td rowspan="3">1</td><td>サイア<br>アルファ<br>ダム</td>
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


def test_recent_distance_with_turn_direction():
    """実サイトの近走は "1400右ダ" のように距離と種別の間に方向が入る（#124）。

    方向を許さないと右回り・左回りが軒並み落ち、方向表記の無いばんえい
    "200ダ" や直線だけが残る。実測でこの欠損が 81.8% あった。
    """
    from ingest.parse import parse_horse_records
    html = _record_html(
        "牡3 鹿毛 55 中島龍 （金沢） 【 10.0% 】 【 30.0% 】 調教師",
        ["2 9頭 金沢 26.08.04 ３歳Ｂ７ 1400右ダ 2人 中島龍 55.0 1:35.1",
         "10 12頭 名古屋 26.07.01 ３歳７ 1500左芝 12人 渡邊竜 55.0 1:39.1"])
    r = parse_horse_records(html, "金沢")[0]
    assert len(r["recent"]) == 2
    assert r["recent"][0]["distance"] == 1400
    assert r["recent"][0]["surface"] == "ダ"
    assert r["recent"][1]["distance"] == 1500
    assert r["recent"][1]["surface"] == "芝"


@pytest.mark.parametrize("cell,dist,surf", [
    ("1 10頭 大井 26.07.13 特別 1400右ダ 1人 騎手 55 1:24.0", 1400, "ダ"),
    ("1 10頭 大井 26.07.13 特別 1600左芝 1人 騎手 55 1:24.0", 1600, "芝"),
    ("1 10頭 大井 26.07.13 特別 1000直ダ 1人 騎手 55 1:24.0", 1000, "ダ"),
    # 方向が無い形（ばんえい・従来から取れていた）も壊さない
    ("1 10頭 帯広 26.07.13 特別 200ダ 1人 騎手 570 2:24.0", 200, "ダ"),
    ("1 10頭 大井 26.07.13 特別 1200 芝 1人 騎手 55 1:24.0", 1200, "芝"),
])
def test_recent_distance_variants(cell, dist, surf):
    from ingest.parse import parse_horse_records
    html = _record_html("牡3 鹿毛 55 騎手 （大井） 【 1.0% 】 【 2.0% 】 師",
                        [cell])
    got = parse_horse_records(html, "大井")[0]["recent"][0]
    assert got["distance"] == dist and got["surface"] == surf


def test_horse_records_shared_position_cell():
    """同一枠に複数頭いる場合、2頭目以降は枠番セルが省略される（#114）。

    実サイトでは枠番セルが同枠の代表行にしか出ず rowspan で束ねられるため、
    2頭目の <tr> はヘッダー基準の固定インデックスだと1列ズレる。class 属性
    （number/name/profile/race）を優先して読むことで、ズレの影響を受けずに
    2頭目も正しく拾えることを確認する。
    """
    from ingest.parse import parse_horse_records
    html = """
    <table>
      <tr><th>枠番</th><th>馬番</th><th>父馬 馬名 母馬</th>
          <th>性齢 毛色 負担重量 騎手名 【勝率】 【3着内率】</th>
          <th>前5走 着順 頭数 競走日 距離</th><th>成績</th></tr>
      <tr><td rowspan="6">1</td><td rowspan="3" class="number">1</td>
          <td class="name">サイア<br>アルファ<br>ダム</td>
          <td class="profile">牝3 鹿毛 54 田中一 （大井） 【 10.0% 】 【 20.0% 】 佐藤次</td>
          <td class="race place01">6 10頭 大井 26.07.13 芝S 1200ダ 4人 田中一 54 1:12.0</td>
          <td>0 0 0</td></tr>
      <tr><td rowspan="3" class="number">2</td>
          <td class="name">サイア2<br>ベータ<br>ダム2</td>
          <td class="profile">牡4 栗毛 56 鈴木三 （大井） 【 15.0% 】 【 30.0% 】 山田四</td>
          <td class="race place01">3 12頭 大井 26.07.06 芝S 1400芝 2人 鈴木三 56 1:24.0</td>
          <td>0 0 0</td></tr>
    </table>"""
    recs = parse_horse_records(html, "大井")
    assert [r["num"] for r in recs] == [1, 2]
    r2 = recs[1]
    assert r2["name"] == "ベータ" and r2["jockey"] == "鈴木三"
    assert r2["weight_carried"] == 56
    assert len(r2["recent"]) == 1


def test_horse_records_decimal_weight():
    """斤量が小数表記（実サイトの標準形）でも騎手名を斤量と取り違えない。"""
    from ingest.parse import parse_horse_records
    html = _record_html(
        "牡2 栗毛 54.0 木間龍 （船　橋） 【 0.0% 】 【 0.0% 】 渡邊貴",
        [])
    recs = parse_horse_records(html, "船橋")
    r = recs[0]
    assert r["weight_carried"] == 54
    assert r["jockey"] == "木間龍"


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
