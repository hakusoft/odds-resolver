"""HTML パーサ。取得元 DOM 構造に依存する部分をここに閉じ込める。
想定外の構造なら空/None を返し、呼び出し側で欠測扱いにする。
"""
import re

from bs4 import BeautifulSoup

from .venues import venue_from_key

_KEY_RE = re.compile(r"/race_card/list/RACEID/(\d{18})")

# 騎手名として数値だけの文字列が入っていたら、それは斤量の取り違え（#112/#118）。
# 型は正常な文字列なので後段は警告なく騎手名として扱ってしまう。欠測なら弾ける
# が汚染は弾けないため、パースの時点で落とす。
_NUMERIC_RE = re.compile(r"^\d+(\.\d+)?$")


def is_contaminated_jockey(value) -> bool:
    """騎手名が数値だけなら汚染（斤量が入り込んでいる）。

    実在する騎手名は 85 種すべて漢字（`▲南部楓` のような減量記号つきを含む）で、
    数字のみのものは無い。判定に weight_carried との一致を使わないのは、斤量側も
    壊れている例があるため（実測 1813 件中 16 件が斤量 10〜13 という有り得ない値）。
    """
    if value is None:
        return False
    return bool(_NUMERIC_RE.match(str(value).strip()))


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


def _parse_place(cell: str) -> dict | None:
    """複勝オッズのセルを {lo, hi} に読む（#89）。

    複勝は他の着順の組み合わせで配当が動くため、取得元は単一値ではなく
    「2.2-4.4」のような**範囲**で出す。単勝と同じ float 列には収まらない。
    発売直後は「0.0-0.0」になりうるので、単勝と同じく 0 は「まだ無い」= None。
    """
    m = re.match(r"^([\d.]+)\s*-\s*([\d.]+)$", cell)
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    if not lo or not hi:
        return None
    return {"lo": lo, "hi": hi}


def parse_odds(html: str) -> dict | None:
    """オッズページから馬(num,name)と単勝・複勝オッズを返す。

    {horses:[{num,name}], odds:[float|None], place:[{lo,hi}|None]}。
    想定外の構造なら None。

    複勝は同じページの同じ表に載っており**追加リクエストは不要**（#89）。
    ただし範囲なので odds とは別の列として持つ。複勝列が無いページ形でも
    壊れないよう、place は取れた時だけ埋める（取れなければ全て None）。
    """
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table", class_="dataTable"):
        head = [c.get_text(strip=True) for c in t.find_all("tr")[0].find_all(["th", "td"])]
        if "馬番" in head and "単勝オッズ" in head:
            table = t
            col = {h.replace(" ", ""): i for i, h in enumerate(head)}
            break
    if not table:
        return None

    i_num = col.get("馬番")
    i_name = col.get("馬名")
    i_odds = col.get("単勝オッズ")
    i_place = col.get("複勝オッズ")
    if i_num is None or i_name is None or i_odds is None:
        return None

    horses, odds, place = [], [], []
    for tr in table.find_all("tr")[1:]:
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) <= max(i_num, i_name, i_odds):
            continue
        m = re.match(r"^\d+$", cells[i_num])
        if not m:
            continue
        horses.append({"num": int(cells[i_num]), "name": cells[i_name]})
        mo = re.match(r"^([\d.]+)$", cells[i_odds])
        # 発売直後の未投票馬は「0.0」と表示される。0 はオッズとして
        # 存在しない値なので「まだ無い」= None に落とす
        v = float(mo.group(1)) if mo else None
        odds.append(v if v else None)
        place.append(_parse_place(cells[i_place])
                     if i_place is not None and len(cells) > i_place else None)
    if not horses:
        return None
    order = sorted(range(len(horses)), key=lambda k: horses[k]["num"])
    return {
        "horses": [horses[k] for k in order],
        "odds": [odds[k] for k in order],
        "place": [place[k] for k in order],
    }


def parse_result(html: str) -> list[dict] | None:
    """結果ページから着順を返す: [{pos, num}]（表の行順 = 着順）。
    中止・取消・失格など非数値着順の行は含めない。想定外の構造なら None。
    """
    soup = BeautifulSoup(html, "html.parser")
    for t in soup.find_all("table", class_="dataTable"):
        rows = t.find_all("tr")
        if not rows:
            continue
        head = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if "着順" not in head or "馬番" not in head:
            continue
        i_pos, i_num = head.index("着順"), head.index("馬番")
        finish = []
        for tr in rows[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) <= max(i_pos, i_num):
                continue
            if not (cells[i_pos].isdigit() and cells[i_num].isdigit()):
                continue
            finish.append({"pos": int(cells[i_pos]), "num": int(cells[i_num])})
        return finish or None
    return None


# 馬柱（競走馬の素性・近走）を抜くための正規表現。会場に依存しない形で
# 「あるものだけ拾う」。想定外のトークンは自然に無視される（#55）。
_SEXAGE_RE = re.compile(r"([牡牝セせん騙]\d+)")
_PCT_RE = re.compile(r"【\s*([\d.]+)\s*%\s*】")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{2})")
_POS_FIELD_RE = re.compile(r"^(\d+)\D*?(\d+)\s*頭")
# 近走の距離・馬場。実表記は "1400右ダ" のように距離と種別の間に回り方向が
# 入る（#124）。方向を許さないと右回り・左回りが軒並み落ち、方向表記の無い
# ばんえい "200ダ" や直線だけが残る（実測 18.2%）。
_DIST_RE = re.compile(r"(\d{3,4})\s*[右左直]?\s*(ダ|芝)")
_POP_RE = re.compile(r"(\d+)\s*人")
_TIME_RE = re.compile(r"(\d{1,2}:\d{2}\.\d)")


def parse_horse_records(html: str, venue: str) -> list[dict] | None:
    """馬柱テーブルから各馬の素性・近走を返す（馬番昇順）。

    研究（Benter 等）が重要とする近走成績・騎手勝率・斤量・性齢を中核に、
    堅牢に抜けるものは全て拾う。会場（帯広ばんえい/通常競馬）に依存せず、
    ヘッダー列名でインデックスを引き、値は正規表現で抜く。想定外の構造なら
    None（呼び出し側で欠測扱い）。venue は将来の会場別対応のフックとして
    受けるが、第一版のロジックでは使わない。
    """
    soup = BeautifulSoup(html, "html.parser")
    table = _find_record_table(soup)
    if not table:
        return None

    rows = table.find_all("tr")
    header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    col = _record_columns(header)
    if "num" not in col:
        return None

    records = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        # 1頭分は馬番セルに rowspan（実測 3）が付き、複数 <tr> にまたがる。
        # find_all("tr") は rowspan を解決しないため、中間行（「持ち時計」等）を
        # 拾うとセル位置がずれて誤読する。さらに「枠番」セルは同枠に複数頭いる
        # 場合は代表行にしか出ず rowspan で束ねられるため、2頭目以降の行では
        # ヘッダー基準の固定インデックス（col["num"]）が1列ズレる。
        # 実サイトは td/th に class="number" 等の役割名を振っているため、
        # 引ければそれを優先し、無いフォーマット（テスト用合成 HTML 等）では
        # 従来どおりヘッダー基準の固定インデックスにフォールバックする。
        row_col = _row_columns_by_class(cells) or col
        if "num" not in row_col or len(cells) <= row_col["num"]:
            continue
        num_cell = cells[row_col["num"]]
        if not num_cell.get("rowspan"):
            continue
        num_txt = num_cell.get_text(strip=True)
        if not re.match(r"^\d+$", num_txt):
            continue
        records.append(_parse_record_row(cells, row_col, num_txt))
    if not records:
        return None
    records.sort(key=lambda r: r["num"])
    return records


_CLASS_TO_ROLE = {
    "number": "num",
    "name": "pedigree",
    "profile": "profile",
}


def _row_columns_by_class(cells) -> dict | None:
    """行内セルの class 属性から役割 → インデックスを引く。

    枠番セルの省略で列がズレても、class は役割どおりに振られているため
    影響を受けない。近走セルはヘッダーでは class="lately" 1列だが、データ
    行では開催場ごとに class="race placeNN" と複数列に展開される（成績列も
    同じく複数列）。そのため recent_start は「先頭が race クラスの最初の
    セル」で特定する。class が見つからなければ None（フォールバック指示）。
    """
    col = {}
    for i, c in enumerate(cells):
        classes = c.get("class") or []
        for cls in classes:
            role = _CLASS_TO_ROLE.get(cls)
            if role and role not in col:
                col[role] = i
        if "race" in classes and "recent_start" not in col:
            col["recent_start"] = i
    return col or None


def _find_record_table(soup):
    for t in soup.find_all("table"):
        txt = t.get_text(" ", strip=True)
        if "騎手" in txt and "馬名" in txt and ("前5走" in txt or "前走" in txt):
            return t
    return None


def _record_columns(header: list[str]) -> dict:
    """ヘッダー文字列から列の役割 → インデックスを引く。

    ヘッダーの「前5走…」は 1 列に集約されているが、データ行では近走が
    複数列に分かれる。そこで profile 列（性齢・斤量・勝率を含む）と
    近走の開始列（ヘッダーに「前5走」を含む列）だけをヘッダーから特定し、
    近走の終端はデータ行側で「成績/全成績」列の手前まで、と決める。
    """
    col = {}
    for i, h in enumerate(header):
        h2 = h.replace(" ", "")
        if h2 == "馬番":
            col["num"] = i
        elif "父馬" in h2 and "母馬" in h2:
            col["pedigree"] = i
        elif "性齢" in h2 and "騎手" in h2:
            col["profile"] = i
        elif "前5走" in h2 or "前５走" in h2:
            col["recent_start"] = i
        elif ("全成績" in h2 or h2 == "成績") and "recent_end" not in col:
            col["recent_end"] = i  # 近走はこの列の手前まで
    return col


def _parse_record_row(cells, col: dict, num_txt: str) -> dict:
    rec = {"num": int(num_txt)}
    if "pedigree" in col:
        parts = cells[col["pedigree"]].get_text("|", strip=True).split("|")
        # 父馬 | 馬名 | 母馬 | ... の並び。馬名は2番目
        if len(parts) >= 2:
            rec["name"] = parts[1]
        if len(parts) >= 1:
            rec["sire"] = parts[0]
        if len(parts) >= 3:
            rec["dam"] = parts[2]
    if "profile" in col:
        _parse_profile(cells[col["profile"]], rec)
    # 近走はヘッダーの colspan とデータ行の列数が食い違う（成績列が
    # データ側で複数列に展開される）ため、位置でなく中身で判定する。
    # 近走セルは必ず「N頭」と日付を持つ。それを満たすセルだけ拾う。
    if "recent_start" in col:
        recent = []
        for c in cells[col["recent_start"]:]:
            txt = c.get_text(" ", strip=True)
            if "頭" not in txt or not _DATE_RE.search(txt):
                continue
            r = _parse_recent(c)
            if r:
                recent.append(r)
        rec["recent"] = recent[:5]
    return rec


def _parse_profile(cell, rec: dict):
    """性齢・毛色・斤量・騎手・勝率・3着内率・調教師を素性列から抜く。"""
    txt = cell.get_text(" ", strip=True)
    m = _SEXAGE_RE.search(txt)
    if m:
        rec["sex_age"] = m.group(1)
    pcts = _PCT_RE.findall(txt)
    if len(pcts) >= 2:
        rec["jockey_win_pct"] = float(pcts[0])
        rec["jockey_top3_pct"] = float(pcts[1])
    # 斤量: 【】より前の 2-3 桁数字（性齢の後の最初の数字）
    head = txt.split("【")[0]
    mw = re.search(r"(\d{2,3})", head)
    if mw:
        rec["weight_carried"] = int(mw.group(1))
    # 騎手名: 【】直前のトークン（毛色・斤量・数字を除いた最後の非数字語）
    toks = [t for t in re.split(r"[\s（）]", head) if t]
    names = [t for t in toks if not re.match(r"^\d+(\.\d+)?$", t)
             and not _SEXAGE_RE.match(t)]
    if len(names) >= 2 and not is_contaminated_jockey(names[1]):
        rec["jockey"] = names[1]  # [毛色, 騎手] の並び


def _parse_recent(cell) -> dict | None:
    """近走1走分から着順・頭数・日付・距離・馬場・人気・タイムを抜く。"""
    txt = cell.get_text(" ", strip=True)
    if not txt or txt in ("-", "－"):
        return None
    r = {}
    mp = _POS_FIELD_RE.match(txt)
    if mp:
        r["pos"] = int(mp.group(1))
        r["field_size"] = int(mp.group(2))
    md = _DATE_RE.search(txt)
    if md:
        r["date"] = md.group(1)
    mdist = _DIST_RE.search(txt)
    if mdist:
        r["distance"] = int(mdist.group(1))
        r["surface"] = mdist.group(2)
    mpop = _POP_RE.search(txt)
    if mpop:
        r["popularity"] = int(mpop.group(1))
    mt = _TIME_RE.search(txt)
    if mt:
        r["time"] = mt.group(1)
    # 着順も日付も取れない = 実質空の走は含めない
    if "pos" not in r and "date" not in r:
        return None
    return r


# 組合せ馬券のオッズ（#56）。行列形式で載る。
_PAIR_ODDS_RE = re.compile(r"^\d+(\.\d+)?$")


def parse_exotic_matrix(html: str) -> dict | None:
    """馬単・馬複の行列オッズを {(1着, 2着): odds} で返す（#56）。

    実サイトの表は「列ヘッダー = 1 着馬番」「各行に (2着馬番, オッズ) の
    ペアが列数ぶん並ぶ」形。同じ馬番の交点は `-`、未発売は `0.0`。

        1     2     3
      1  -  1 0.0  1 574.5     ← 行内に (2着馬番, オッズ) が繰り返される
      2 0.0  2  -  2 574.5

    0.0 は None にする（単勝と同じ扱い。発売前は「まだ無い」であって
    「人気が無い」ではない）。想定外の構造なら None。

    馬複も同じ形で載るが、i<j の片側にしか値が無い点だけ違う。呼び出し側で
    対称化するかは用途次第なのでここでは触らない。

    **列が多いと表が横に分割される。** 9 頭立ての馬単は 1〜8 列目と 9 列目が
    別テーブルになっていた。全ての行列テーブルをマージしないと、最後の列が
    まるごと欠ける（実測で 72 組中 64 組しか取れなかった）。
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for table in soup.find_all("table", class_="dataTable"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        head = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if not head or not all(h.isdigit() for h in head):
            continue  # 1 着馬番のヘッダーでなければ別の表
        firsts = [int(h) for h in head]
        for tr in rows[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            # (2着馬番, オッズ) のペアが列数ぶん。足りなければ構造が違う
            if len(cells) < 2 * len(firsts):
                continue
            for i, first in enumerate(firsts):
                num_s, odds_s = cells[2 * i], cells[2 * i + 1]
                if not num_s.isdigit():
                    continue
                second = int(num_s)
                if first == second:
                    continue  # 交点（`-`）
                if not _PAIR_ODDS_RE.match(odds_s):
                    continue
                v = float(odds_s)
                out[(first, second)] = v if v > 0 else None
    return out or None
