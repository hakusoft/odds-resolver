"""毎分フェッチャ: 締切駆動の段階制で1レースのオッズを取る（Issue #18）。

EventBridge が毎分起動。1 起動で最大 1 リクエスト（Crawl-Delay 60 を
構造で保証）。DynamoDB の当日レース表を読み、発走時刻と現在時刻・前回
取得時刻から最も切迫したレースを 1 つ選んで取得し、RACE#{race_id} に
TS#{HH:MM} で append する。

段階制（発走 T までの残り分 → 望ましい取得間隔）:
  T-45〜T-20 : 15 分毎
  T-20〜T-10 :  5 分毎
  T-10〜T    :  2 分毎
  T〜T+3     :  1 回だけ（確定オッズ）
  それ以外   : 対象外
数字は仮置き。実測（#23）で調整する。
"""
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

from . import source
from .parse import parse_odds

_TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

# 移送前の生存期間。夜間バッチで S3 へ焼いた後も翌々日まで残す
_TTL_DAYS = 2


def _now():
    return time.time()


def jst_hm(ts: float) -> str:
    lt = time.gmtime(ts + 9 * 3600)
    return f"{lt.tm_hour}:{lt.tm_min:02d}"


def jst_today(ts: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts + 9 * 3600))


def _post_epoch(date: str, post_time: str) -> float:
    """YYYYMMDD と HH:MM（JST）を UTC epoch に。calendar.timegm は UTC 基準
    なので、JST の時刻から 9 時間引いて渡す。"""
    import calendar
    h, m = (int(x) for x in post_time.split(":"))
    y, mo, d = int(date[:4]), int(date[4:6]), int(date[6:8])
    return calendar.timegm((y, mo, d, h - 9, m, 0, 0, 0, 0))


def _desired_interval_sec(minutes_to_post: float) -> float | None:
    """発走までの残り分 → 望ましい取得間隔（秒）。対象外なら None。"""
    if 20 <= minutes_to_post <= 45:
        return 15 * 60
    if 10 <= minutes_to_post < 20:
        return 5 * 60
    if 0 <= minutes_to_post < 10:
        return 2 * 60
    if -3 <= minutes_to_post < 0:
        return None  # 発走後は下の確定取得で扱う
    return None


def _races_today(date: str) -> list[dict]:
    items = _TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"DAY#{date}")
    ).get("Items", [])
    return items


def _last_snapshot_ts(race_id: str) -> float | None:
    items = _TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"RACE#{race_id}"),
        ScanIndexForward=False, Limit=1,
    ).get("Items", [])
    return float(items[0]["fetched_at"]) if items else None


def _has_final(race_id: str) -> bool:
    items = _TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"RACE#{race_id}"),
        ScanIndexForward=False, Limit=1,
    ).get("Items", [])
    return bool(items and items[0].get("final"))


def _pick(now: float, races: list[dict]) -> tuple[dict, bool] | None:
    """取得すべき 1 レースと「確定取得か」を返す。無ければ None。

    切迫度 = 望ましい間隔をどれだけ超過したか。最大のものを選ぶ。
    発走直後（T〜T+3）で未確定のレースは最優先で確定を取る。
    """
    best = None  # (score, race, is_final)
    for r in races:
        date = r["race_id"][:8]
        try:
            post = _post_epoch(date, r["post_time"])
        except Exception:
            continue
        minutes = (post - now) / 60.0

        # 発走直後の確定取得（1 回だけ）
        if -3 <= minutes < 0 and not _has_final(r["race_id"]):
            return r, True

        interval = _desired_interval_sec(minutes)
        if interval is None:
            continue
        last = _last_snapshot_ts(r["race_id"])
        elapsed = (now - last) if last else 1e9
        if elapsed < interval:
            continue
        score = elapsed - interval  # 超過が大きいほど切迫
        # 締切が近いほど僅かに優先（同点時の tie-break）
        score += max(0, (45 - minutes)) * 0.1
        if best is None or score > best[0]:
            best = (score, r, False)
    if best is None:
        return None
    return best[1], best[2]



def run(now: float | None = None) -> dict:
    now = now or _now()
    date = jst_today(now)
    races = _races_today(date)
    if not races:
        return {"date": date, "picked": None, "note": "no races"}

    picked = _pick(now, races)
    if picked is None:
        return {"date": date, "picked": None}
    race, is_final = picked

    key = race.get("source_key")
    if not key:
        return {"date": date, "picked": race["race_id"], "note": "no source_key"}

    html = source.fetch(source.odds_path(key))
    parsed = parse_odds(html)
    if parsed is None:
        return {"date": date, "picked": race["race_id"], "note": "parse failed"}

    _append_snapshot(race["race_id"], now, parsed, is_final)
    return {"date": date, "picked": race["race_id"],
            "time": jst_hm(now), "final": is_final,
            "horses": len(parsed["horses"])}


def _append_snapshot(race_id: str, now: float, parsed: dict, is_final: bool):
    from decimal import Decimal
    expires_at = int(now) + _TTL_DAYS * 24 * 3600
    item = {
        "pk": f"RACE#{race_id}",
        "sk": f"TS#{jst_hm(now)}",
        "time": jst_hm(now),
        "fetched_at": Decimal(str(int(now))),
        "horses": parsed["horses"],
        "odds": [Decimal(str(o)) if o is not None else None for o in parsed["odds"]],
        "expires_at": expires_at,
    }
    if is_final:
        item["final"] = True
    _TABLE.put_item(Item=item)


def handler(event, context):
    return run()
