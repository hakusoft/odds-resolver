"""夜間バッチ: 確定した当日分を S3 view へ焼く（Issue #22）。

23:30 JST 起動。DynamoDB のホットデータを読み取り API と同一スキーマの
JSON にして S3 へ書く。整形は api.py の関数をそのまま使い、当日（API）と
過去（S3）でスキーマが乖離しない構造にする。

書き先は 2 つ:
  - data バケット: 正本（append-only・バージョニング）。ルート直下に置く
  - frontend バケット: 配信用。CloudFront が読む data/ プレフィクス配下

days.json（日付目次）は data バケット側を正とし read-modify-write で
更新する。書き手はこのバッチだけなので競合しない。

翌 0:15 の朝ジョブより先に走る順序が前提（日付切替の空白防止）。
非開催日（器なし）は何もしない。DynamoDB からの削除は書かない（TTL 任せ）。
"""
import json
import os
import time

import boto3

from .api import _index, _race

# キャッシュ方針: 目次類は短く、確定レースは長く（deploy.yml と同方針）
_CC_DAYS = "public, max-age=60"
_CC_DAY_INDEX = "public, max-age=3600"
_CC_RACE = "public, max-age=86400"

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def jst_today() -> str:
    return time.strftime("%Y%m%d", time.gmtime(time.time() + 9 * 3600))


def run(date: str | None = None) -> dict:
    date = date or jst_today()
    try:
        index = _index(date)
    except KeyError:
        return {"date": date, "races": 0, "note": "no races"}

    for r in index["races"]:
        race = _race(r["race_id"])
        _put(f"races/{r['race_id']}.json", race, _CC_RACE)
    _put(f"{date}/index.json", index, _CC_DAY_INDEX)

    days = _update_days(index)
    return {"date": date, "races": len(index["races"]), "days": len(days)}


def _put(key: str, body: dict, cache_control: str):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    for bucket, prefix in (
        (os.environ["DATA_BUCKET"], ""),
        (os.environ["FRONTEND_BUCKET"], "data/"),
    ):
        _get_s3().put_object(
            Bucket=bucket, Key=prefix + key, Body=data,
            ContentType="application/json; charset=utf-8",
            CacheControl=cache_control,
        )


def _update_days(index: dict) -> list[dict]:
    days = _load_days()
    venues = list(dict.fromkeys(r["venue"] for r in index["races"]))
    entry = {
        "date": index["date"],
        "venues": venues,
        "n_venues": len(venues),
        "n_races": len(index["races"]),
    }
    days = [d for d in days if d["date"] != entry["date"]]
    days.append(entry)
    days.sort(key=lambda d: d["date"], reverse=True)
    _put("days.json", {"days": days}, _CC_DAYS)
    return days


def _load_days() -> list[dict]:
    try:
        res = _get_s3().get_object(Bucket=os.environ["DATA_BUCKET"], Key="days.json")
        return json.loads(res["Body"].read()).get("days", [])
    except _get_s3().exceptions.NoSuchKey:
        return []


def handler(event, context):
    if isinstance(event, dict) and event.get("mode") == "yesterday":
        # 朝の窓で回収した前日の着順を view へ反映する再焼き（Issue #52）
        return run(time.strftime("%Y%m%d",
                                 time.gmtime(time.time() + 9 * 3600 - 24 * 3600)))
    date = event.get("date") if isinstance(event, dict) else None
    return run(date)
