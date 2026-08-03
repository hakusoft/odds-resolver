"""当日読み取り API（Issue #19）。Lambda Function URL で公開する。

DynamoDB のホットデータを、S3 の view JSON と同一スキーマで返す。
フロントは当日=この API / 過去=S3 を、URL だけの違いで読み分けられる。

  GET /?date=YYYYMMDD   → 日別 index（S3 の {date}/index.json と同形）
  GET /?id=RACE_ID      → レース詳細（S3 の races/{id}.json と同形）

指標（top1/ent）は snapshots があれば計算して index に載せる。
オッズがまだ無い器の段階では snapshots は空・指標なし。
"""
import json
import os

import boto3
from boto3.dynamodb.conditions import Key

from .metrics import support_metrics

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table

_HEADERS = {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "cache-control": "public, max-age=60",
}


def handler(event, context):
    params = (event or {}).get("queryStringParameters") or {}
    try:
        if params.get("id"):
            body = _race(params["id"])
        elif params.get("date"):
            body = _index(params["date"])
        else:
            return _resp(400, {"error": "date または id が必要です"})
    except KeyError:
        return _resp(404, {"error": "not found"})
    return _resp(200, body)


def _index(date: str) -> dict:
    """日別 index。DAY#{date} のレース表 + 各レースの最新指標。"""
    items = _query(f"DAY#{date}")
    if not items:
        raise KeyError(date)
    races = []
    for it in sorted(items, key=lambda x: (x["venue"], int(x["race_no"]))):
        r = {
            "race_id": it["race_id"], "venue": it["venue"],
            "race_no": int(it["race_no"]), "name": it["name"],
            "post_time": it["post_time"], "n_horses": int(it["n_horses"]),
            "surface": it.get("surface"),
            "distance": int(it["distance"]) if it.get("distance") is not None else None,
        }
        if it.get("top1") is not None:
            # フェッチャが書き込み時に前計算した値（Issue #48）。追加クエリ不要
            r["top1"], r["ent"] = float(it["top1"]), float(it["ent"])
        else:
            # 前計算導入前に書かれた器へのフォールバック（TTL 2 日で自然消滅）
            metrics = _latest_metrics(it["race_id"])
            if metrics:
                r["top1"], r["ent"] = metrics
        races.append(r)
    return {"date": f"{date[:4]}-{date[4:6]}-{date[6:]}", "races": races}


def _race(rid: str) -> dict:
    """レース詳細。器（DAY 側）+ 全スナップショット（RACE 側）。"""
    date = rid[:8]
    meta = None
    for it in _query(f"DAY#{date}"):
        if it["race_id"] == rid:
            meta = it
            break
    if meta is None:
        raise KeyError(rid)

    items = _query(f"RACE#{rid}")
    result = next((i for i in items if i["sk"] == "RESULT"), None)
    snaps = [i for i in items if i["sk"].startswith("TS#")]
    horses, snapshots = [], []
    if snaps:
        snaps.sort(key=lambda x: x["sk"])
        first = snaps[0]
        horses = [{"num": int(h["num"]), "name": h["name"]} for h in first["horses"]]
        snapshots = [{
            "time": s["time"],
            **({"slot": s["slot"]} if "slot" in s else {}),
            "odds": [float(o) if o else None for o in s["odds"]],
            # 複勝は範囲（#89）。取り始める前の snapshot には無いので、
            # 有る時だけ載せる（古い view と混ざっても壊れない）
            **({"place": [{"lo": float(p["lo"]), "hi": float(p["hi"])}
                          if p else None for p in s["place"]]}
               if s.get("place") else {}),
        } for s in snaps]

    out = {
        "race_id": rid, "name": meta["name"], "venue": meta["venue"],
        "race_no": int(meta["race_no"]), "post_time": meta["post_time"],
        "surface": meta.get("surface"),
        "distance": int(meta["distance"]) if meta.get("distance") is not None else None,
        "horses": horses, "snapshots": snapshots,
    }
    if result:
        out["result"] = [{"pos": int(f["pos"]), "num": int(f["num"])}
                         for f in result["finish"]]
    if meta.get("records"):
        # 馬柱（#55）。DynamoDB の Decimal を素の数値へ戻して露出する。
        # archive は api の整形を共用するため、これで S3 view にも一緒に焼かれる
        out["records"] = _plain(meta["records"])
    # 前向き検証の予測記録（#106）。判定時点の値だけを持ち、結果は含まない。
    # 同じ query で取れるので追加クエリは要らない
    signals = [i for i in items if i["sk"].startswith("SIGNAL#")]
    if signals:
        signals.sort(key=lambda x: x["sk"])
        out["signals"] = [_plain({k: v for k, v in s.items()
                                  if k not in ("pk", "sk", "expires_at")})
                          for s in signals]
    return out


def _plain(obj):
    """DynamoDB の Decimal を int/float へ戻す（JSON 化のため・#55）。"""
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, list):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


def _latest_metrics(rid: str):
    """最新スナップショットの支持率から top1/ent を計算。無ければ None。

    sk を TS# に絞るのが要。RESULT 項目と同居しているため、スナップ
    ショット 0 件のレースで降順 Limit 1 が RESULT を掴む事故を防ぐ（#63）。
    """
    snaps = _query(f"RACE#{rid}", limit=1, desc=True, sk_prefix="TS#")
    if not snaps:
        return None
    return support_metrics(snaps[0]["odds"])


def _query(pk: str, limit: int | None = None, desc: bool = False,
           sk_prefix: str | None = None):
    cond = Key("pk").eq(pk)
    if sk_prefix:
        cond = cond & Key("sk").begins_with(sk_prefix)
    kw = {"KeyConditionExpression": cond}
    if limit:
        kw["Limit"] = limit
    if desc:
        kw["ScanIndexForward"] = False
    return _get_table().query(**kw).get("Items", [])


def _resp(status: int, body: dict):
    return {"statusCode": status, "headers": _HEADERS,
            "body": json.dumps(body, ensure_ascii=False)}
