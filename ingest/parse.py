"""HTML パーサ。取得元 DOM 構造に依存する部分をここに閉じ込める。
想定外の構造なら空/None を返し、呼び出し側で欠測扱いにする。
"""
import re

from bs4 import BeautifulSoup

from .venues import venue_from_key

_KEY_RE = re.compile(r"/race_card/list/RACEID/(\d{18})")


def parse_day_list(html: str, date: str) -> list[dict]:
    """当日開催の一覧から各会場の代表キーと会場名を返す: [{venue, key}]

    会場名は場コード表から引く（RACEID の 9-10 桁目）。リンクテキストの
    表記ゆれ（「大井ナイター」等）に依存しない。
    """
    soup = BeautifulSoup(html, "html.parser")
    reps = {}
    for a in soup.find_all("a", href=True):
        m = _KEY_RE.search(a["href"])
        if not m:
            continue
        key = m.group(1)
        if not key.startswith(date) or not key.endswith("00"):
            continue
        venue = venue_from_key(key)
        if venue:
            reps.setdefault(key, venue)
    return [{"venue": v, "key": k} for k, v in reps.items()]


def parse_race_list(html: str, venue: str) -> list[dict]:
    """会場のレース一覧テーブルから各レースのメタを返す:
    [{race_no, post_time, name, n_horses, surface, distance}]

    ヘッダー行の列名でインデックスを引く。列順が変わっても壊れない。
    """
    soup = BeautifulSoup(html, "html.parser")
    table = _find_race_table(soup)
    if not table:
        return []

    rows = table.find_all("tr")
    header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    col = _column_index(header)
    if "race_no" not in col:
        return []

    races = []
    for tr in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        row = _parse_row(cells, col)
        if row:
            races.append(row)
    return races


def _find_race_table(soup):
    for table in soup.find_all("table"):
        txt = table.get_text(" ", strip=True)
        if "距離" in txt and "頭数" in txt and "発走" in txt:
            return table
    return None


def _column_index(header: list[str]) -> dict:
    """ヘッダー文字列から列の役割 → インデックスを引く。"""
    col = {}
    for i, h in enumerate(header):
        h = h.replace(" ", "")
        if h == "レース":
            col["race_no"] = i
        elif "発走" in h:
            col["post_time"] = i
        elif "レース名" in h:
            col["name"] = i
        elif "距離" in h:
            col["dist"] = i
        elif "頭数" in h:
            col["heads"] = i
    return col


def _parse_row(cells: list[str], col: dict) -> dict | None:
    if len(cells) <= max(col.values()):
        return None
    mrn = re.match(r"(\d{1,2})", cells[col["race_no"]].replace(" ", ""))
    if not mrn:
        return None
    rno = int(mrn.group(1))

    post = ""
    if "post_time" in col:
        mt = re.search(r"(\d{1,2}):(\d{2})", cells[col["post_time"]])
        if mt:
            post = f"{int(mt.group(1))}:{mt.group(2)}"

    surface = distance = None
    if "dist" in col:
        md = re.search(r"(芝|ダ)\s*([0-9,]+)m", cells[col["dist"]])
        if md:
            surface = md.group(1)
            distance = int(md.group(2).replace(",", ""))

    n_horses = 0
    if "heads" in col:
        mh = re.search(r"(\d+)", cells[col["heads"]])
        if mh:
            n_horses = int(mh.group(1))

    name = cells[col["name"]].strip() if "name" in col else ""

    return {
        "race_no": rno, "post_time": post, "name": name,
        "n_horses": n_horses, "surface": surface, "distance": distance,
    }
