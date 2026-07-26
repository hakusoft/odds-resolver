"""朝ジョブ: 当日のレース表を取得し DynamoDB に器を作る（Issue #17）。

日付が変わった直後（0:15 JST）に起動。当日のレース一覧
（レース ID・会場・レース番号・発走時刻・レース名・頭数・馬場・距離）を
集めて DynamoDB に DAY#{date} パーティションで書き込む。

書き込むだけ。オッズ取得（毎分フェッチャ）はこの器を前提に別途動く。
非開催日・取得失敗はその日を静かに諦める（append-only ゆえ翌日に影響しない）。
"""
import os
import time

import boto3

from . import source
from .parse import parse_day_list, parse_race_list
from .race_id import make_race_id

_TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

# 移送前の生存期間。夜間バッチ(23:30)で S3 へ焼いた後も翌々日まで残し、
# 事故時に読み直せるようにする（切替直後の空白防止は焼く順序側で担保）
_TTL_DAYS = 2


def jst_today() -> str:
    return time.strftime("%Y%m%d", time.gmtime(time.time() + 9 * 3600))


def run(date: str | None = None) -> dict:
    date = date or jst_today()

    day_html = source.fetch(source.day_list_path(date))
    venues = parse_day_list(day_html, date)
    if not venues:
        return {"date": date, "venues": 0, "races": 0, "note": "no meeting"}

    expires_at = int(time.time()) + _TTL_DAYS * 24 * 3600
    n_races = 0
    for v in venues:
        try:
            list_html = source.fetch(source.race_list_path(v["key"]))
            races = parse_race_list(list_html, v["venue"])
        except Exception:
            # 会場単位の失敗は握って次へ。欠けた会場は翌日以降に影響しない
            continue
        for r in races:
            rid = make_race_id(date, v["venue"], r["race_no"])
            _put_race(date, rid, v["venue"], r, expires_at)
            n_races += 1

    return {"date": date, "venues": len(venues), "races": n_races}


def _put_race(date, rid, venue, r, expires_at):
    _TABLE.put_item(Item={
        "pk": f"DAY#{date}",
        "sk": f"RACE#{rid}",
        "race_id": rid,
        "venue": venue,
        "race_no": r["race_no"],
        "post_time": r["post_time"],
        "name": r["name"],
        "n_horses": r["n_horses"],
        "surface": r["surface"],
        "distance": r["distance"],
        "expires_at": expires_at,
    })


def handler(event, context):
    date = event.get("date") if isinstance(event, dict) else None
    return run(date)
