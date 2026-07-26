"""取得元の場コード ⇔ 会場名 ⇔ サイトスラッグ の対応表。

場コードは RACEID の 9-10 桁目（例 20260724-20-...→ "20"=大井）。
実データで確認済みのコードには verified=True。未確認は稼働時に要検証。
"""

# (場コード, 会場名, サイトスラッグ, 実データ確認済みか)
_VENUES = [
    ("30", "帯広ば", "ob", False),
    ("36", "門別", "mb", True),
    ("10", "盛岡", "mo", False),
    ("11", "水沢", "mz", False),
    ("18", "浦和", "ur", False),
    ("19", "船橋", "fn", False),
    ("20", "大井", "oi", True),
    ("21", "川崎", "kw", False),
    ("22", "金沢", "kz", False),
    ("23", "笠松", "ks", True),
    ("24", "名古屋", "ng", False),
    ("27", "園田", "sd", True),
    ("28", "姫路", "hm", False),
    ("31", "高知", "ko", False),
    ("32", "佐賀", "sg", False),
]

CODE_TO_VENUE = {c: v for c, v, _, _ in _VENUES}
VENUE_TO_SLUG = {v: s for _, v, s, _ in _VENUES}


def venue_from_key(race_key: str) -> str | None:
    """RACEID（18桁）から会場名を返す。未知コードは None。"""
    return CODE_TO_VENUE.get(race_key[8:10])
