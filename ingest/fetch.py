"""毎分フェッチャ: チェックポイント（スロット）駆動で1レースのオッズを取る。

EventBridge が毎分起動。1 起動で最大 1 リクエスト（Crawl-Delay 60 を
構造で保証）。全レース共通のスロット表（発走までの残り分 T− で定義）を
基準に「次のスロットを最も過ぎているレース」を選んで取得し、
RACE#{race_id} に TS#{HH:MM} + slot ラベルで append する（Issue #47）。

スロット表（T− 分, 許容窓 = 次スロットまでの間隔）:
  ベースライン : T-480 〜 T-60 の毎時（8:00 JST 以降のみ試行）
  勝負どころ   : T-45, T-30, T-20, T-15, T-10, T-8, T-6, T-4, T-2
  確定         : 発走直後に 1 回（slot "F"）

選択の優先順位は 確定 > 勝負どころ > ベースライン の段階制。同段では
「スロット超過 ÷ 許容窓」の比率が最大のレースを選ぶ。取得できなかった
スロットは埋め戻さない（明示的な欠測として残す）。発売前の空振りは
DAY 器の last_attempt に記録し 30 分のクールダウンを置く。

スロットの消化状況は DAY 器の closed_slots で追跡する。読みは毎分
1 クエリ（DAY 全件）だけで済み、レースごとの履歴クエリを持たない。
"""
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

from . import source
from .metrics import support_metrics
from .parse import parse_odds, parse_result

_TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

# 移送前の生存期間。夜間バッチで S3 へ焼いた後も翌々日まで残す
_TTL_DAYS = 2

# (発走までの残り分, 許容窓[分])。昇順。数字は仮置きで実測 #23 で調整する
_SLOTS = [
    (2, 2), (4, 2), (6, 2), (8, 2), (10, 2),
    (15, 5), (20, 5), (30, 10), (45, 15),
    (60, 60), (120, 60), (180, 60), (240, 60),
    (300, 60), (360, 60), (420, 60), (480, 60),
]
_BASELINE_MIN = 60          # これ以遠のスロットはベースライン層
# JST。当日の発売開始は 10:00（8:00 開始で運用した 2026-07-27 朝、
# 8:30〜の掃引が全て発売前空振りだったため引き上げ）
_BASELINE_START_HOUR = 10
_ATTEMPT_COOLDOWN_SEC = 30 * 60


def _now():
    return time.time()


def jst_hm(ts: float) -> str:
    lt = time.gmtime(ts + 9 * 3600)
    return f"{lt.tm_hour}:{lt.tm_min:02d}"


def jst_today(ts: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts + 9 * 3600))


def jst_hour(ts: float) -> int:
    return time.gmtime(ts + 9 * 3600).tm_hour


def _post_epoch(date: str, post_time: str) -> float:
    """YYYYMMDD と HH:MM（JST）を UTC epoch に。calendar.timegm は UTC 基準
    なので、JST の時刻から 9 時間引いて渡す。"""
    import calendar
    h, m = (int(x) for x in post_time.split(":"))
    y, mo, d = int(date[:4]), int(date[4:6]), int(date[6:8])
    return calendar.timegm((y, mo, d, h - 9, m, 0, 0, 0, 0))


def _slot_label(minutes: int) -> str:
    return f"T-{minutes}"


def _actionable_slot(minutes_to_post: float, closed: set) -> tuple[int, int] | None:
    """いま試行対象のスロット (T−分, 許容窓) を返す。

    「期限が来ている中で最も発走に近いスロット」= T−分が最小のもの。
    消化済み（closed_slots）ならこのレースに今やることは無い。
    """
    for s, spacing in _SLOTS:
        if s >= minutes_to_post:
            if _slot_label(s) in closed:
                return None
            return s, spacing
    return None  # 最遠スロットよりまだ手前


def _races_today(date: str) -> list[dict]:
    return _TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"DAY#{date}")
    ).get("Items", [])


def _has_final(race_id: str) -> bool:
    # sk を TS# に絞る。RESULT 項目と同居しているため（#63）
    items = _TABLE.query(
        KeyConditionExpression=(
            Key("pk").eq(f"RACE#{race_id}") & Key("sk").begins_with("TS#")),
        ScanIndexForward=False, Limit=1,
    ).get("Items", [])
    return bool(items and items[0].get("final"))


def _pick(now: float, races: list[dict]) -> tuple[dict, int | None, bool] | None:
    """取得すべき (レース, スロットT−分, 確定取得か) を返す。無ければ None。

    優先順位: 確定 > 勝負どころ > ベースライン。
    同段内は「スロット超過 ÷ 許容窓」の比率最大（同点は発走が近い方）。
    ベースラインは発売開始（10:00 JST）以降のみ・空振り後 30 分は再試行しない。
    """
    near = base = None  # (score, race, slot)
    for r in races:
        try:
            post = _post_epoch(r["race_id"][:8], r["post_time"])
        except Exception:
            continue
        minutes = (post - now) / 60.0

        # 発走直後の確定取得（1 回だけ・最優先）
        if -3 <= minutes < 0 and not _has_final(r["race_id"]):
            return r, None, True
        if minutes < 0:
            continue

        closed = set(r.get("closed_slots") or [])
        slot = _actionable_slot(minutes, closed)
        if slot is None:
            continue
        s, spacing = slot
        score = (s - minutes) / spacing  # スロット超過の比率
        score += max(0.0, (480 - minutes)) * 1e-6  # 同点時は発走が近い方
        if s >= _BASELINE_MIN:
            if jst_hour(now) < _BASELINE_START_HOUR:
                continue
            last = r.get("last_attempt")
            if last is not None and now - float(last) < _ATTEMPT_COOLDOWN_SEC:
                continue
            if base is None or score > base[0]:
                base = (score, r, s)
        else:
            if near is None or score > near[0]:
                near = (score, r, s)
    hit = near or base
    if hit is None:
        return None
    return hit[1], hit[2], False


def _pick_result(now: float) -> dict | None:
    """前日→前々日の順で、着順が未回収のレースを発走順に 1 つ返す。

    朝の窓（発売開始前）だけの仕事。前々日まで見るのは、取り逃しの
    自己修復と初回移行のため（DAY 器の TTL が 2 日なのでそれ以遠は無い）。
    """
    if jst_hour(now) >= _BASELINE_START_HOUR:
        return None
    for back in (1, 2):
        day = jst_today(now - back * 24 * 3600)
        cands = []
        for r in _races_today(day):
            if r.get("result_ok") or not r.get("source_key"):
                continue
            last = r.get("result_attempt")
            if last is not None and now - float(last) < _ATTEMPT_COOLDOWN_SEC:
                continue
            cands.append(r)
        if cands:
            return min(cands, key=lambda r: r["post_time"])
    return None


def _run_result(now: float, date: str) -> dict:
    """オッズの仕事が無い分の空きで、前日結果を 1 レース回収する。"""
    race = _pick_result(now)
    if race is None:
        return {"date": date, "picked": None}
    html = source.fetch(source.result_path(race["source_key"]))
    finish = parse_result(html)
    if finish is None:
        _record_attempt(race, now, attr="result_attempt")
        return {"date": date, "picked": race["race_id"], "note": "result parse failed"}
    _put_result(race["race_id"], finish, now)
    race["result_ok"] = True
    race.pop("result_attempt", None)
    _TABLE.put_item(Item=race)
    return {"date": date, "picked": race["race_id"],
            "result": len(finish)}


def run(now: float | None = None) -> dict:
    now = now or _now()
    date = jst_today(now)
    races = _races_today(date)

    picked = _pick(now, races) if races else None
    if picked is None:
        return _run_result(now, date)
    race, slot, is_final = picked

    key = race.get("source_key")
    if not key:
        return {"date": date, "picked": race["race_id"], "note": "no source_key"}

    html = source.fetch(source.odds_path(key))
    parsed = parse_odds(html)
    if parsed is None:
        # 発売前の空振り。ベースライン層のみクールダウンを記録する
        if slot is not None and slot >= _BASELINE_MIN:
            _record_attempt(race, now)
        return {"date": date, "picked": race["race_id"],
                "slot": _slot_label(slot) if slot is not None else "F",
                "note": "parse failed"}

    minutes = (_post_epoch(race["race_id"][:8], race["post_time"]) - now) / 60.0
    label = "F" if is_final else _slot_label(slot)
    _append_snapshot(race["race_id"], now, parsed, is_final, label)
    _update_day_after_snapshot(race, parsed, minutes)
    return {"date": date, "picked": race["race_id"],
            "time": jst_hm(now), "slot": label, "final": is_final,
            "horses": len(parsed["horses"])}


def _append_snapshot(race_id: str, now: float, parsed: dict,
                     is_final: bool, slot_label: str):
    from decimal import Decimal
    expires_at = int(now) + _TTL_DAYS * 24 * 3600
    item = {
        "pk": f"RACE#{race_id}",
        "sk": f"TS#{jst_hm(now)}",
        "time": jst_hm(now),
        "slot": slot_label,
        "fetched_at": Decimal(str(int(now))),
        "horses": parsed["horses"],
        "odds": [Decimal(str(o)) if o is not None else None for o in parsed["odds"]],
        "expires_at": expires_at,
    }
    if is_final:
        item["final"] = True
    _TABLE.put_item(Item=item)


def _update_day_after_snapshot(race: dict, parsed: dict, minutes_to_post: float):
    """DAY 器へ top1/ent の前計算（#48）と消化スロットを書き戻す。

    期限が来ていたスロットは取得の成否に関わらず全て閉じる。取り逃した
    スロットは「閉じているのに対応する snapshot が無い」= 明示的な欠測
    として分析側から見える。器は query 済みの全属性を持っているので
    put_item の全置換で済み、UpdateItem 権限を増やさない。
    """
    from decimal import Decimal
    m = support_metrics(parsed["odds"])
    if m is not None:
        race["top1"] = Decimal(str(m[0]))
        race["ent"] = Decimal(str(m[1]))
    closed = set(race.get("closed_slots") or [])
    closed |= {_slot_label(s) for s, _ in _SLOTS if s >= minutes_to_post}
    race["closed_slots"] = sorted(closed, key=lambda l: int(l[2:]))
    race.pop("last_attempt", None)
    _TABLE.put_item(Item=race)


def _record_attempt(race: dict, now: float, attr: str = "last_attempt"):
    from decimal import Decimal
    race[attr] = Decimal(str(int(now)))
    _TABLE.put_item(Item=race)


def _put_result(race_id: str, finish: list[dict], now: float):
    from decimal import Decimal
    _TABLE.put_item(Item={
        "pk": f"RACE#{race_id}",
        "sk": "RESULT",
        "finish": [{"pos": Decimal(f["pos"]), "num": Decimal(f["num"])}
                   for f in finish],
        "fetched_at": Decimal(str(int(now))),
        "expires_at": int(now) + _TTL_DAYS * 24 * 3600,
    })


def handler(event, context):
    return run()
