"""朝ジョブ: 当日のレース表を取得し DynamoDB に器を作る（Issue #17）。

日付が変わった直後（0:15 JST）に起動。当日のレース一覧
（レース ID・会場・レース番号・発走時刻・レース名・頭数・馬場・距離）を
集めて DynamoDB に DAY#{date} パーティションで書き込む。

書き込むだけ。オッズ取得（毎分フェッチャ）はこの器を前提に別途動く。

**やり直せる形にしてある（#175）。** 0:15〜9:15 JST に毎時起動し、

- 完了印（`MORNING#{date}`）があれば取得元に触れずに終わる
- 既に器がある会場は飛ばし、欠けた会場だけ取り直す
- 全会場が揃ったときだけ完了印を書く

10/22 は取得元が 0:00〜9:40 に 503 を返し、1 回きりの朝ジョブが落ちて
その日が丸ごと欠けた。平常時は 0:15 の 1 回で完了印が付くので、取得元に
触るのはその 1 回だけ。
"""
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

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

    if _is_done(date):
        return {"date": date, "note": "already done"}

    # トップの失敗は握らない。Lambda の非同期リトライと次の毎時起動に任せる
    day_html = source.fetch(source.day_list_path(date))
    venues = parse_day_list(day_html, date)
    expires_at = int(time.time()) + _TTL_DAYS * 24 * 3600
    if not venues:
        # 非開催日も印を付ける。付けないと毎時トップを叩き続ける
        _mark_done(date, 0, expires_at)
        return {"date": date, "venues": 0, "races": 0, "note": "no meeting"}

    have = _venues_present(date)
    n_races = n_failed = 0
    for v in venues:
        if v["venue"] in have:
            continue
        try:
            list_html = source.fetch(source.race_list_path(v["key"]))
            races = parse_race_list(list_html, v["venue"])
        except Exception:
            # 会場単位の失敗は握って次へ。印を付けないので次の起動で拾う
            n_failed += 1
            continue
        for r in races:
            rid = make_race_id(date, v["venue"], r["race_no"])
            source_key = v["key"][:-2] + f"{r['race_no']:02d}"
            _put_race(date, rid, v["venue"], r, source_key, expires_at)
            n_races += 1

    if n_failed == 0:
        _mark_done(date, len(venues), expires_at)
    return {"date": date, "venues": len(venues), "races": n_races,
            "skipped": len(have), "failed": n_failed}


# 完了印は DAY# と別のパーティションに置く。fetch は DAY#{date} を全件読んで
# レースとして扱うので、同居させると器が 1 件増えたように見える
def _done_key(date: str) -> dict:
    return {"pk": f"MORNING#{date}", "sk": "DONE"}


def _is_done(date: str) -> bool:
    # GetItem ではなく Query。ingest ロールに与えているのは PutItem と Query だけ
    return bool(_TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"MORNING#{date}"),
        Limit=1).get("Items"))


def _mark_done(date: str, n_venues: int, expires_at: int):
    _TABLE.put_item(Item={**_done_key(date), "venues": n_venues,
                          "at": int(time.time()), "expires_at": expires_at})


def _venues_present(date: str) -> set[str]:
    """既に器がある会場。途中で落ちた回の続きから取るため。"""
    items, kw = [], {"KeyConditionExpression": Key("pk").eq(f"DAY#{date}")}
    while True:
        res = _TABLE.query(**kw)
        items += res.get("Items", [])
        if "LastEvaluatedKey" not in res:
            break
        kw["ExclusiveStartKey"] = res["LastEvaluatedKey"]
    return {it["venue"] for it in items if it.get("venue")}


def _put_race(date, rid, venue, r, source_key, expires_at):
    _TABLE.put_item(Item={
        "pk": f"DAY#{date}",
        "sk": f"RACE#{rid}",
        "race_id": rid,
        "source_key": source_key,
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
