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
from .form import is_edge_pick, race_edges
from .metrics import support_metrics
from .parse import (parse_exotic_matrix, parse_horse_records, parse_odds,
                    parse_result)
from .surge import detect_surges

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


def _pick_record(now: float, races: list[dict]) -> dict | None:
    """当日レースで馬柱が未取得のものを発走順に 1 つ返す（朝の窓限定）。"""
    if jst_hour(now) >= _BASELINE_START_HOUR:
        return None
    cands = []
    for r in races:
        if r.get("record_ok") or not r.get("source_key"):
            continue
        last = r.get("record_attempt")
        if last is not None and now - float(last) < _ATTEMPT_COOLDOWN_SEC:
            continue
        cands.append(r)
    return min(cands, key=lambda r: r["post_time"]) if cands else None


def _run_record(now: float, date: str, races: list[dict]) -> dict | None:
    """朝の窓の空きで、当日レースの馬柱を 1 レース取得する。無ければ None。"""
    race = _pick_record(now, races)
    if race is None:
        return None
    html = source.fetch(source.record_path(race["source_key"]))
    records = parse_horse_records(html, race["venue"])
    if records is None:
        _record_attempt(race, now, attr="record_attempt")
        return {"date": date, "picked": race["race_id"], "note": "record parse failed"}
    # スナップショット側の horses（出走馬 num/name）と役割が違うので
    # 馬柱は records で持つ（衝突回避・#55）
    race["records"] = _decimalize(records)
    race["record_ok"] = True
    race.pop("record_attempt", None)
    _TABLE.put_item(Item=race)
    return {"date": date, "picked": race["race_id"], "record": len(records)}


def _decimalize(obj):
    """馬柱の float（勝率など）を DynamoDB 用に Decimal へ再帰変換する。"""
    from decimal import Decimal
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, list):
        return [_decimalize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _decimalize(v) for k, v in obj.items()}
    return obj


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
        # 単複の仕事が無い分の空き。組合せオッズ（#56）を優先するのは、
        # 締切間際という時間の制約があるため。馬柱と着順回収は朝の窓に
        # 余裕があり、後回しにしても取り逃さない
        return (_run_exotic(now, date, races)
                or _run_record(now, date, races)
                or _run_result(now, date))
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
    prev = _latest_snapshot_odds(race["race_id"])  # 追記前に前回を取る
    _append_snapshot(race["race_id"], now, parsed, is_final, label)
    _notify_surges(race, prev, parsed, minutes)
    _record_edges(race, parsed, minutes, now)
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
    # 複勝は範囲（lo-hi）なので odds とは別列で持つ（#89）。取得元の表に
    # 複勝列が無い形（旧パーサ・想定外の構造）でも壊れないよう、全て None
    # なら書かない = 既存スキーマのままにする
    place = parsed.get("place") or []
    if any(p is not None for p in place):
        item["place"] = [
            {"lo": Decimal(str(p["lo"])), "hi": Decimal(str(p["hi"]))}
            if p is not None else None for p in place
        ]
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


def _latest_snapshot_odds(race_id: str) -> list | None:
    """直近スナップショットのオッズ列を返す（急変判定の基準・#71）。"""
    items = _TABLE.query(
        KeyConditionExpression=(
            Key("pk").eq(f"RACE#{race_id}") & Key("sk").begins_with("TS#")),
        ScanIndexForward=False, Limit=1,
    ).get("Items", [])
    if not items:
        return None
    return [float(o) if o is not None else None for o in items[0]["odds"]]


def _notify_surges(race: dict, prev_odds: list | None, parsed: dict,
                   minutes_to_post: float):
    """支持率が急上昇した馬を SNS へ通知する（#71）。

    重複抑制は DAY 器の surged（通知済み馬番）で行う。race dict に
    surged を積んでおけば直後の _update_day_after_snapshot の
    put_item（全置換）で一緒に永続化され、追加の書き込みが要らない。
    SNS 未設定（トピック ARN 無し）なら判定だけして送らない。
    """
    from decimal import Decimal
    surges = detect_surges(prev_odds, parsed["odds"], parsed["horses"],
                           minutes_to_post)
    if not surges:
        return
    already = {int(n) for n in (race.get("surged") or [])}
    fresh = [s for s in surges if s["num"] not in already]
    if not fresh:
        return
    # 記録が先。通知（SNS 未設定なら何もしない）に成否を左右させない
    _record_signal(race, fresh, minutes_to_post, parsed, time.time())
    _publish_surges(race, fresh, minutes_to_post)
    race["surged"] = sorted(already | {s["num"] for s in fresh})
    race["surged"] = [Decimal(n) for n in race["surged"]]


def _publish_surges(race: dict, surges: list, minutes_to_post: float):
    topic = os.environ.get("SURGE_TOPIC_ARN")
    if not topic:
        return  # 通知先未設定。判定・記録は済ませ、送信だけしない
    lines = [f"{race['venue']} {int(race['race_no'])}R "
             f"（発走 {race['post_time']}・あと約{int(minutes_to_post)}分）",
             "支持率が急上昇しました:"]
    for s in surges:
        lines.append(f"  {s['num']} {s['name']}  "
                     f"{s['prev']*100:.0f}% → {s['curr']*100:.0f}% "
                     f"(+{s['delta']*100:.0f}pt)")
    lines.append("")
    lines.append("※取得は最大2分遅れ・締切に間に合う保証はありません。"
                 "馬券判断は自己責任で。")
    boto3.client("sns").publish(
        TopicArn=topic,
        Subject=f"急変 {race['venue']}{int(race['race_no'])}R",
        Message="\n".join(lines))


def _record_attempt(race: dict, now: float, attr: str = "last_attempt"):
    from decimal import Decimal
    race[attr] = Decimal(str(int(now)))
    _TABLE.put_item(Item=race)


def _record_signal(race: dict, surges: list, minutes_to_post: float,
                   parsed: dict, now: float):
    """急変を検知した**その時点**の予測を記録する（#106・前向き検証）。

    較正（#53 以降）は結果が出た後に遡って集計するので、「良い帯を探して
    見つけた」以上のことが言えない。後から的を描いていないことを示すには、
    **予測を結果より先に確定させ、後から書き換えない**記録が要る。

    ここは急変が確定する唯一の地点なので、通知と同じ場所で残す。判定に
    使った支持率も一緒に書く（後で帯を切り直せるように。結果を見てから
    帯の定義を動かすと、それ自体が後知恵になる）。

    sk は SIGNAL# 前置。RACE# 配下に TS#/RESULT と同居するため、参照側が
    begins_with で種類を明示できるようにする（#63 の教訓）。
    DynamoDB は TTL 2 日なので、夜間バッチが S3 へ焼いて永続化する。
    """
    from decimal import Decimal
    rid = race.get("race_id")
    sup = _support_of(parsed["odds"])
    # 記録できない事情（ID 欠落・オッズ全滅）があっても通知は止めない。
    # 検証用の記録は「あれば嬉しい」もので、本番の通知より優先度が低い
    if not rid or sup is None:
        return
    num_to_idx = {int(h["num"]): i for i, h in enumerate(parsed["horses"])}
    for s in surges:
        i = num_to_idx.get(int(s["num"]))
        if i is None:
            continue
        _TABLE.put_item(Item={
            "pk": f"RACE#{rid}",
            "sk": f"SIGNAL#{int(s['num']):02d}",
            "num": Decimal(int(s["num"])),
            "name": s["name"],
            # 判定時点の値。結果は入れない（答え合わせは後日 archive が行う）
            "support": Decimal(str(round(sup[i], 4))),
            "odds": Decimal(str(parsed["odds"][i])) if parsed["odds"][i] else None,
            "delta": Decimal(str(s["delta"])),
            "slot_minutes": Decimal(int(minutes_to_post)),
            "signaled_at": Decimal(str(int(now))),
            "expires_at": int(now) + _TTL_DAYS * 24 * 3600,
        })


def _support_of(odds: list) -> list[float] | None:
    inv = [(1.0 / float(o) if o else 0.0) for o in odds]
    s = sum(inv)
    return [x / s for x in inv] if s > 0 else None


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


# 乖離スコアを記録する締切前スロット（分）。ここより手前では市場がまだ
# 値を決めきっていないので、比べても意味が薄い。
EDGE_SLOT_MINUTES = 10


def _record_edges(race: dict, parsed: dict, minutes_to_post: float, now: float):
    """二軸の乖離が閾値を超えた馬を、**その時点**で記録する（#117 Phase 2-3）。

    #106 の SIGNAL# と同じ狙い。較正は結果を見てから遡るので「良い帯を探して
    見つけた」以上のことが言えない。ここは**予測が結果より先に確定していた**
    ことが構造的に保証される記録を作る。

    急変シグナル（SIGNAL#）との違い:

    - 急変はイベント駆動（起きた時だけ）だが、乖離は毎回計算できる。
      閾値（EDGE_THRESHOLD）を超えた馬だけ書く
    - 1 レース 1 回だけ書く。毎スロット書くと同じ馬が何度も入り、
      n が水増しされる。締切間際の 1 点に固定する

    **馬柱（records）が無いレースは記録しない。** 推定できないものを
    無理に出さない（#117 Phase 1-4）。
    """
    from decimal import Decimal
    rid = race.get("race_id")
    records = race.get("records")
    if not rid or not records:
        return
    # 締切間際の 1 点だけ。早すぎる時間帯は市場が固まっていない
    if minutes_to_post > EDGE_SLOT_MINUTES:
        return
    if race.get("edge_logged"):
        return

    edges = race_edges(_undecimalize(records), parsed["odds"],
                       parsed["horses"])
    picks = [e for e in edges if is_edge_pick(e["edge"])]
    race["edge_logged"] = True
    if not picks:
        return

    name_of = {int(h["num"]): h.get("name") for h in parsed["horses"]
               if h.get("num") is not None}
    odds_of = {int(h["num"]): parsed["odds"][i]
               for i, h in enumerate(parsed["horses"])
               if h.get("num") is not None and i < len(parsed["odds"])}
    for e in picks:
        num = int(e["num"])
        o = odds_of.get(num)
        _TABLE.put_item(Item={
            "pk": f"RACE#{rid}",
            "sk": f"EDGE#{num:02d}",
            "num": Decimal(num),
            "name": name_of.get(num),
            # 判定時点の値。結果は入れない（答え合わせは archive が後日行う）
            "p_form": Decimal(str(round(e["p_form"], 5))),
            "p_market": Decimal(str(round(e["p_market"], 5))),
            # 素のオッズ。p_market は正規化済みで控除率込みの元値に戻せない
            # ため、回収率の計算にはこれが要る（判定の前提・#117 Phase 3）
            "odds": Decimal(str(o)) if o else None,
            "edge": Decimal(str(round(e["edge"], 4))),
            "form_score": (Decimal(str(round(e["score"], 4)))
                           if e["score"] is not None else None),
            "slot_minutes": Decimal(int(minutes_to_post)),
            "signaled_at": Decimal(str(int(now))),
            "expires_at": int(now) + _TTL_DAYS * 24 * 3600,
        })


def _undecimalize(v):
    """DynamoDB の Decimal を float/int に戻す。form は素の数値を前提にする。"""
    from decimal import Decimal
    if isinstance(v, list):
        return [_undecimalize(x) for x in v]
    if isinstance(v, dict):
        return {k: _undecimalize(x) for k, x in v.items()}
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    return v


# 組合せ馬券を取る券種と順序（#56）。馬単と三連複でまず筋を確かめる。
# 三連単は 617KB/レースと重いので、2 券種の結果を見てから判断する。
EXOTIC_KINDS = ("umatan", "sanrenfuku")

# 組合せを取る締切前スロット（分）。単勝の乖離（EDGE_SLOT_MINUTES）と
# 揃える。同じ瞬間の単勝と組合せを比べたいので、時点をずらさない。
EXOTIC_SLOT_MINUTES = EDGE_SLOT_MINUTES


def _pick_exotic(now: float, races: list[dict]) -> tuple[dict, str] | None:
    """組合せオッズを取るべき (レース, 券種) を返す。無ければ None。

    締切間際の 1 点だけ取る。**1 レース 1 券種 1 回**で、取得済みは
    DAY 器の exotic_done で追跡する（surged / edge_logged と同じ手口で、
    直後の put_item に相乗りするので追加の書き込みが要らない）。

    発走済みは取らない。締切を過ぎたオッズは確定値だが、**予測が結果より
    先に確定していた**という担保が崩れる。
    """
    for r in races:
        if not r.get("source_key"):
            continue
        try:
            post = _post_epoch(r["race_id"][:8], r["post_time"])
        except Exception:
            continue
        minutes = (post - now) / 60.0
        if minutes <= 0 or minutes > EXOTIC_SLOT_MINUTES:
            continue
        done = set(r.get("exotic_done") or [])
        for kind in EXOTIC_KINDS:
            if kind not in done:
                return r, kind
    return None


def _run_exotic(now: float, date: str, races: list[dict]) -> dict | None:
    """空き時間で組合せオッズを 1 券種取る。無ければ None。

    単複のスロットを圧迫しない。#56 の実測では全 6 券種を各 1 回取っても
    270 req/日で、1440 の枠に対して余裕がある。
    """
    picked = _pick_exotic(now, races)
    if picked is None:
        return None
    race, kind = picked
    html = source.fetch(source.exotic_path(kind, race["source_key"]))
    matrix = parse_exotic_matrix(html)

    done = list(race.get("exotic_done") or [])
    done.append(kind)
    race["exotic_done"] = done
    if matrix:
        # 組は "1-2" のような文字列キーにする（DynamoDB はタプルを持てない）
        race.setdefault("exotic", {})[kind] = _decimalize(
            {f"{a}-{b}": v for (a, b), v in matrix.items() if v})
    _TABLE.put_item(Item=race)
    return {"date": date, "picked": race["race_id"], "exotic": kind,
            "pairs": len(matrix) if matrix else 0}
