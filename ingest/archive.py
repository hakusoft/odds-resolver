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
from .metrics import (CALIB_BINS, calibration_bins, classify_race, place_bins)
from .surge import (LATE_WINDOW, SURGE_DELTA, SURGE_MIN_SLOT, early_mask,
                    late_mask, persist_mask, revert_mask, surged_mask)

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

    day_calib = _empty_calib_set()
    n_scored = 0
    n_banei_scored = 0  # ばんえい（#109）は平地の n_races と別カウント
    classes = []  # 当日総括（#83）用の分類。追加クエリなしで同ループから
    fetched = []  # 前向き検証（#106）も同じ _race の結果を使い回す
    for r in index["races"]:
        race = _race(r["race_id"])
        fetched.append(race)
        _put(f"races/{r['race_id']}.json", race, _CC_RACE)
        if _accumulate_calib(day_calib, race):
            if race.get("venue") == _BANEI_VENUE:
                n_banei_scored += 1
            else:
                n_scored += 1
        c = classify_race(race)
        if c is not None:
            classes.append({"race_id": r["race_id"], "venue": r["venue"],
                            "race_no": r["race_no"], **c})
    index["summary"] = _summarize(classes)
    _put(f"{date}/index.json", index, _CC_DAY_INDEX)

    days = _update_days(index)
    _update_calibration(date, day_calib, n_scored, n_banei_scored)
    n_signals = _append_forward_log(date, fetched)
    n_edges = _append_edge_log(date, fetched)
    _update_status(date, index, fetched, days)
    return {"date": date, "races": len(index["races"]), "days": len(days),
            "calib_races": n_scored, "banei_races": n_banei_scored,
            "signals": n_signals, "edges": n_edges}


def recalc(date: str) -> dict:
    """S3 に焼いた races/*.json から、その日の較正だけを積み直す（#69）。

    run() は DynamoDB の DAY 器を起点にするため、TTL(2日)が切れた日は
    再焼きできない（`no races` になる）。だが S3 の view には snapshots と
    result が残っているので、較正の再集計だけなら復元できる。

    集計ロジックを変えた時（例: #87/#88 で系統を 3→7 に増やした時）に、
    過去日を新しい定義で埋め直すための経路。**races/*.json は書き換えない** —
    読むだけで、更新するのは calibration.json の by_date[date] のみ。
    view を焼き直さないので CloudFront の invalidation も不要。
    """
    races = _load_day_races(date)
    if not races:
        return {"date": date, "races": 0, "note": "no archived races"}
    day_calib = _empty_calib_set()
    n_scored = 0
    n_banei_scored = 0
    for race in races:
        if _accumulate_calib(day_calib, race):
            if race.get("venue") == _BANEI_VENUE:
                n_banei_scored += 1
            else:
                n_scored += 1
    _update_calibration(date, day_calib, n_scored, n_banei_scored)
    return {"date": date, "races": len(races), "calib_races": n_scored,
            "banei_races": n_banei_scored, "source": "s3"}


def _append_forward_log(date: str, races: list[dict]) -> int:
    """予測（signals）に結果を突き合わせ、前向き検証ログへ追記する（#106）。

    較正（#53 以降）は結果を見てから遡って集計するため、「良い帯を探して
    見つけた」以上のことが言えない。ここは **予測が結果より先に確定して
    いた**ことが構造的に保証されるログを作る。fetch が締切前に書いた
    SIGNAL# をそのまま持ち込み、着順だけを後から付ける。

    日ごとに 1 ファイル（forward/{date}.json）。**既にあれば書かない** —
    再焼きで結果が変わることはないし、上書きを許すと「後から書き換えて
    いない」という前提が崩れる。検証の値打ちはそこにしかない。
    """
    key = f"forward/{date}.json"
    if _exists(key):
        return 0
    rows = []
    for race in races:
        signals = race.get("signals")
        if not signals:
            continue
        result = race.get("result") or []
        pos = {r["num"]: r["pos"] for r in result}
        for s in signals:
            num = int(s["num"])
            rows.append({
                "race_id": race["race_id"],
                "venue": race.get("venue"),
                "num": num,
                "name": s.get("name"),
                # --- 予測時点（fetch が締切前に確定させた値）---
                "support": s.get("support"),
                "odds": s.get("odds"),
                "delta": s.get("delta"),
                "slot_minutes": s.get("slot_minutes"),
                "signaled_at": s.get("signaled_at"),
                # --- 結果（後から付けるのはここだけ）---
                "pos": pos.get(num),
                "won": pos.get(num) == 1,
                "top3": pos.get(num) is not None and pos[num] <= 3,
            })
    if not rows:
        return 0
    _put(key, {"date": date, "n": len(rows), "rows": rows}, _CC_RACE)
    return len(rows)


def _append_edge_log(date: str, races: list[dict]) -> int:
    """二軸の乖離（edges）に結果を突き合わせ、前向きログへ焼く（#117 Phase 2-3）。

    `_append_forward_log`（#106 の急変シグナル）と同じ構造・同じ理由。fetch が
    締切前に書いた EDGE# をそのまま持ち込み、着順だけを後から付ける。

    **過去分は作れない。** EDGE# は fetch がその場で書くものなので、遡って
    生成する経路が無い。これは制約ではなく担保で、「予測が結果より先に確定
    していた」ことが構造的に保証される。

    `edge/{date}.json` へ **write-once**。既にあれば書かない — 上書きを許すと
    「後から書き換えていない」という前提が崩れ、検証の値打ちが消える。
    """
    key = f"edge/{date}.json"
    if _exists(key):
        return 0
    rows = []
    for race in races:
        edges = race.get("edges")
        if not edges:
            continue
        result = race.get("result") or []
        pos = {r["num"]: r["pos"] for r in result}
        for e in edges:
            num = int(e["num"])
            rows.append({
                "race_id": race["race_id"],
                "venue": race.get("venue"),
                "num": num,
                "name": e.get("name"),
                # --- 予測時点（fetch が締切前に確定させた値）---
                "p_form": e.get("p_form"),
                "p_market": e.get("p_market"),
                # 回収率の計算に要る。p_market からは復元できない
                "odds": e.get("odds"),
                "edge": e.get("edge"),
                "form_score": e.get("form_score"),
                "slot_minutes": e.get("slot_minutes"),
                "signaled_at": e.get("signaled_at"),
                # --- 結果（後から付けるのはここだけ）---
                "pos": pos.get(num),
                "won": pos.get(num) == 1,
                "top3": pos.get(num) is not None and pos[num] <= 3,
            })
    if not rows:
        return 0
    _put(key, {"date": date, "n": len(rows), "rows": rows}, _CC_RACE)
    return len(rows)


def _exists(key: str) -> bool:
    try:
        _get_s3().head_object(Bucket=os.environ["DATA_BUCKET"], Key=key)
        return True
    except Exception:
        return False


def _load_day_races(date: str) -> list[dict]:
    """races/{date}-*.json を読む。ID の先頭 8 桁が日付なので前方一致で引ける。"""
    s3 = _get_s3()
    bucket = os.environ["DATA_BUCKET"]
    out = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": f"races/{date}-"}
        if token:
            kw["ContinuationToken"] = token
        res = s3.list_objects_v2(**kw)
        for obj in res.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            out.append(json.loads(body))
        if not res.get("IsTruncated"):
            break
        token = res.get("NextContinuationToken")
    return out


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


def _summarize(classes: list[dict]) -> dict | None:
    """分類済みレースから当日総括を作る（#83）。終わったレースが無ければ None。

    件数と、導線用の波乱・急変的中レース、最も難しかったレース（ent 最大）を返す。
    """
    if not classes:
        return None

    def _brief(c):
        return {"race_id": c["race_id"], "venue": c["venue"],
                "race_no": c["race_no"], "ent": c["ent"]}

    upsets = [_brief(c) for c in classes if c["upset"]]
    surge_hits = [_brief(c) for c in classes if c["surge_hit"]]
    hardest = max(classes, key=lambda c: c["ent"])
    return {
        "n_races": len(classes),
        "firm": sum(1 for c in classes if c["firm"]),
        "upset": len(upsets),
        "surge_hit": len(surge_hits),
        "upset_races": upsets,
        "surge_hit_races": surge_hits,
        "hardest": _brief(hardest),
    }


def _load_days() -> list[dict]:
    try:
        res = _get_s3().get_object(Bucket=os.environ["DATA_BUCKET"], Key="days.json")
        return json.loads(res["Body"].read()).get("days", [])
    except _get_s3().exceptions.NoSuchKey:
        return []


# ---- 較正曲線の累積（Issue #53） --------------------------------------
# calibration.json を data バケット正本で read-modify-write する。日別の
# 寄与（by_date）を保持し、再焼き（着順の後追い反映）で同じ日を上書き
# しても二重計上しない。全期間の集計は by_date の総和で組み立てる。

# 較正は 7 系統を並行して積む。系統はこのキー順で固定する。
#
#   total / surged / calm            … 全馬 / 急変あり / 急変なし（#76）
#   persist / revert                 … 急変を「持続」と「平均回帰」に割る（#88）
#   late / early                     … 急変を「締切5分以内」と「それ以前」に割る（#87）
#
# kaz の仮説「不人気 × 急変は妙味か」を同じ支持率帯で対比するのが出発点（#76）。
# そこへ 2 本の独立な軸を足した。persist/revert は arXiv:2402.02623 を逆に読んだ
# もの（情報なら跳ねは残り、一時的な大口なら戻る）、late/early は arXiv:2509.14645
# の "final-five-minute" を切るもの。**両者は独立**で交差しうる（早く跳ねて持続、
# 直前に跳ねて回帰、等）。それぞれ surged の内側を排他に二分する。
#
# 帯別に積むのは論文の "similar final odds" と同じ発想で、人気の効果と
# 経路の効果を分離するため。単純比較すると持続組は支持率が構造的に高くなる。
_CALIB_SETS = ("total", "surged", "calm",
               "persist", "revert", "late", "early")

# 複勝（#89）は全馬 / 急変あり / 急変なし の 3 系統だけ積む。持続・回帰や
# 直前・それ以前まで割ると 1 セルが 1 桁になり読めないため、まず粗く見る。
# 標本が貯まってから細分するかを判断する
_PLACE_SETS = ("total", "surged", "calm")

# 帯広ばんえい（#109）は他14場と種目が違う（そりを引き2つの山を越える）ため
# 平地の較正に混ぜない。標本も少ないので複勝と同じく粗く3系統だけ積む
_BANEI_VENUE = "帯広ば"
_BANEI_SETS = ("total", "surged", "calm")


def _empty_bins() -> list[dict]:
    return [{"n": 0, "sum_support": 0.0, "wins": 0, "payback": 0.0}
            for _ in range(len(CALIB_BINS) - 1)]


def _empty_place_bins() -> list[dict]:
    # 単勝は wins（1着）、複勝は hits（3着以内）。名前を分けて取り違えを防ぐ
    return [{"n": 0, "sum_support": 0.0, "hits": 0, "payback": 0.0}
            for _ in range(len(CALIB_BINS) - 1)]


def _empty_calib_set() -> dict:
    acc = {k: _empty_bins() for k in _CALIB_SETS}
    acc["place"] = {k: _empty_place_bins() for k in _PLACE_SETS}
    acc["banei"] = {k: _empty_bins() for k in _BANEI_SETS}
    return acc


def _accumulate_calib(acc: dict, race: dict) -> bool:
    """レース詳細から全馬/急変あり/急変なしの較正を acc に足す。

    着順・オッズが無ければ False。急変判定は surge.py を全スナップ
    ショットに適用（可視化 #73 と同じ定義）。帯広ばんえい（#109）は種目が
    違うため平地の系統には積まず、acc["banei"] へ別集計する。
    """
    if not race.get("result") or not race.get("snapshots"):
        return False
    odds = race["snapshots"][-1]["odds"]
    winner_num = next((r["num"] for r in race["result"] if r["pos"] == 1), None)
    if winner_num is None:
        return False
    winner_idx = next((i for i, h in enumerate(race["horses"])
                       if h["num"] == winner_num), None)
    nh = len(race["horses"])
    snaps = race["snapshots"]
    mask = surged_mask(snaps, nh)
    masks = {
        "total": None,
        "surged": mask,
        "calm": [not m for m in mask],
        "persist": persist_mask(snaps, nh),
        "revert": revert_mask(snaps, nh),
        "late": late_mask(snaps, nh),
        "early": early_mask(snaps, nh),
    }

    is_banei = race.get("venue") == _BANEI_VENUE
    keys = _BANEI_SETS if is_banei else _CALIB_SETS
    dest = acc["banei"] if is_banei else acc
    computed = {}
    for k in keys:
        bins = calibration_bins(odds, winner_idx, masks[k])
        if bins is None:
            return False
        computed[k] = bins
    for k in keys:
        for a, b in zip(dest[k], computed[k]):
            a["n"] += b["n"]
            a["sum_support"] += b["sum_support"]
            a["wins"] += b["wins"]
            a["payback"] += b["payback"]

    # 複勝の較正（#89）。複勝を取り始める前のレースには place が無いので、
    # 有る時だけ積む。単勝側とは母数が別（place が欠けた馬は入らない）。
    # ばんえいも複勝は取っているが、まずは平地側に混ぜず素通りする
    if not is_banei:
        _accumulate_place(acc, race, odds, masks)
    return True


def _accumulate_place(acc: dict, race: dict, odds: list, masks: dict):
    """3 着以内の較正を acc["place"] に足す。place が無ければ何もしない。"""
    snap = race["snapshots"][-1]
    place = snap.get("place")
    if not place or not any(p for p in place):
        return
    top3_nums = {r["num"] for r in race["result"] if r["pos"] <= 3}
    top3_idx = {i for i, h in enumerate(race["horses"])
                if h["num"] in top3_nums}
    for k in _PLACE_SETS:
        bins = place_bins(odds, place, top3_idx, masks[k])
        if bins is None:
            return
        for a, b in zip(acc["place"][k], bins):
            a["n"] += b["n"]
            a["sum_support"] += b["sum_support"]
            a["hits"] += b["hits"]
            a["payback"] += b["payback"]


def _update_calibration(date: str, day_calib: dict, n_races: int,
                        n_banei_races: int = 0):
    doc = _load_calibration()
    # 再焼きで同日を上書きしても二重計上しないよう、日別に置き換える
    doc["by_date"][date] = {"races": n_races, "banei_races": n_banei_races,
                            "sets": day_calib}
    total = _empty_calib_set()
    for entry in doc["by_date"].values():
        sets = entry.get("sets") or {"total": entry.get("bins", _empty_bins())}
        for k in _CALIB_SETS:
            for t, b in zip(total[k], sets.get(k, _empty_bins())):
                t["n"] += b["n"]
                t["sum_support"] += b["sum_support"]
                t["wins"] += b["wins"]
                t["payback"] += b.get("payback", 0.0)
        # 複勝（#89）。取り始める前の日は place を持たないので素通りする
        for k in _PLACE_SETS:
            src = (sets.get("place") or {}).get(k)
            if not src:
                continue
            for t, b in zip(total["place"][k], src):
                t["n"] += b["n"]
                t["sum_support"] += b["sum_support"]
                t["hits"] += b["hits"]
                t["payback"] += b.get("payback", 0.0)
        # 帯広ばんえい（#109）。平地とは別集計なので取り始める前の日は素通り
        for k in _BANEI_SETS:
            src = (sets.get("banei") or {}).get(k)
            if not src:
                continue
            for t, b in zip(total["banei"][k], src):
                t["n"] += b["n"]
                t["sum_support"] += b["sum_support"]
                t["wins"] += b["wins"]
                t["payback"] += b.get("payback", 0.0)
    doc["bin_edges"] = CALIB_BINS
    doc["total"] = _finalize_bins(total["total"])
    doc["by_surge"] = {"surged": _finalize_bins(total["surged"]),
                       "calm": _finalize_bins(total["calm"])}
    # 急変の内訳。持続/回帰（#88）と 直前/それ以前（#87）は独立な軸で、
    # それぞれ surged を排他に二分する。
    #
    # **分母の期間が total/surged/calm とズレる**点に注意。by_date は日別の
    # 集計値だけを持ち生レースを残さないので、この機能より前に焼いた日は
    # 新キーが 0 のまま埋まらない（遡るには当該日を date 指定で再焼きする）。
    # since に「新軸を実際に持つ最初の日」を出し、読む側が期間を合わせられる
    # ようにする。全日が旧形式なら None（0 を実績と誤読させない）。
    detailed = sorted(d for d, e in doc["by_date"].items()
                      if "persist" in (e.get("sets") or {}))
    since = detailed[0] if detailed else None
    doc["by_persistence"] = {"persist": _finalize_bins(total["persist"]),
                             "revert": _finalize_bins(total["revert"]),
                             "since": since}
    doc["by_timing"] = {"late": _finalize_bins(total["late"]),
                        "early": _finalize_bins(total["early"]),
                        "since": since}
    # 複勝（#89）。横軸は単勝支持率のまま、成績だけ 3 着以内に差し替えたもの。
    # 複勝を取り始めた日より前は集計されないので、ここも since を出す
    # 「place キーがある」ではなく「実際に頭数が入っている」で判定する。
    # _empty_calib_set は常に place を作るので、キーの有無では空の日も
    # 拾ってしまい、複勝ゼロ件でも place が出てしまう
    with_place = sorted(
        d for d, e in doc["by_date"].items()
        if any(b["n"] for k, bins in ((e.get("sets") or {}).get("place") or {}).items()
               for b in bins)
    )
    if with_place:
        doc["place"] = {k: _finalize_place(total["place"][k]) for k in _PLACE_SETS}
        doc["place"]["since"] = with_place[0]
    # 帯広ばんえい（#109）。同じ「実際に頭数が入っている」判定で since を出す
    with_banei = sorted(
        d for d, e in doc["by_date"].items()
        if any(b["n"] for k, bins in ((e.get("sets") or {}).get("banei") or {}).items()
               for b in bins)
    )
    if with_banei:
        doc["banei"] = {k: _finalize_bins(total["banei"][k]) for k in _BANEI_SETS}
        doc["banei"]["since"] = with_banei[0]
        doc["banei"]["n_races"] = sum(
            e.get("banei_races", 0) for e in doc["by_date"].values())
    doc["surge_threshold"] = {"min_slot": SURGE_MIN_SLOT, "delta": SURGE_DELTA,
                              "late_window": LATE_WINDOW}
    doc["n_days"] = len(doc["by_date"])
    doc["n_races"] = sum(e["races"] for e in doc["by_date"].values())
    # 毎日更新される累積ファイルなので目次類と同じ短キャッシュにする
    _put("calibration.json", doc, _CC_DAYS)


def _finalize_place(total: list[dict]) -> list[dict]:
    """複勝の集計に (平均支持率, 3着内率, 回収率) を付ける（#89）。

    place_rate は「単勝でこの支持率だった馬が 3 着以内に来た割合」。
    payback は複勝オッズ下限で積んだ安全側の回収率。
    """
    out = []
    for i, b in enumerate(total):
        n = b["n"]
        out.append({
            "lo": CALIB_BINS[i], "hi": CALIB_BINS[i + 1], "n": n,
            "mean_support": round(b["sum_support"] / n, 4) if n else None,
            "place_rate": round(b["hits"] / n, 4) if n else None,
            "payback": round(b["payback"] / n, 4) if n else None,
            "hits": b["hits"],
        })
    return out


def _finalize_bins(total: list[dict]) -> list[dict]:
    """集計から表示用の (平均支持率, 実勝率, 回収率) を付ける。"""
    out = []
    for i, b in enumerate(total):
        n = b["n"]
        out.append({
            "lo": CALIB_BINS[i], "hi": CALIB_BINS[i + 1], "n": n,
            "mean_support": round(b["sum_support"] / n, 4) if n else None,
            "win_rate": round(b["wins"] / n, 4) if n else None,
            "payback": round(b["payback"] / n, 4) if n else None,
            "wins": b["wins"],
        })
    return out


def _load_calibration() -> dict:
    try:
        res = _get_s3().get_object(Bucket=os.environ["DATA_BUCKET"],
                                   Key="calibration.json")
        doc = json.loads(res["Body"].read())
        doc.setdefault("by_date", {})
        return doc
    except _get_s3().exceptions.NoSuchKey:
        return {"by_date": {}}


def _invalidate(date: str) -> str | None:
    """再焼きで上書きした日の CloudFront キャッシュを無効化する（Issue #64）。

    レース詳細は 24 時間キャッシュのため、上書き（着順の追記など）は
    invalidation しないと最大 1 日見えない。ワイルドカード 1 パス扱い ×
    2 パス/日で、無料枠 1,000 パス/月に対し余裕。
    """
    dist = os.environ.get("DISTRIBUTION_ID")
    if not dist:
        return None  # ローカル実行など。焼き自体は成立している
    ref = f"rebake-{date}-{int(time.time())}"
    boto3.client("cloudfront").create_invalidation(
        DistributionId=dist,
        InvalidationBatch={
            "Paths": {"Quantity": 2,
                      "Items": [f"/data/races/{date}-*",
                                f"/data/{date}/index.json"]},
            "CallerReference": ref,
        })
    return ref


def handler(event, context):
    # S3 起点の較正再集計（#69）。TTL 切れの過去日を新しい集計定義で
    # 埋め直す用。view は触らないので invalidation もしない
    if isinstance(event, dict) and event.get("mode") == "recalc":
        return recalc(event["date"])
    if isinstance(event, dict) and event.get("mode") == "yesterday":
        # 朝の窓で回収した前日の着順を view へ反映する再焼き（Issue #52）
        date = time.strftime("%Y%m%d",
                             time.gmtime(time.time() + 9 * 3600 - 24 * 3600))
    else:
        date = event.get("date") if isinstance(event, dict) else None
    out = run(date)
    # 過去日の焼き直しのみ無効化する。当日の初回焼き（23:30）は上書きでは
    # ないためキャッシュ汚染がなく、不要
    if out.get("races") and date and date != jst_today():
        out["invalidation"] = _invalidate(date)
    return out


# 検証の判定基準（#117 Phase 3）。status.json に載せる残数の計算に使う。
# judge_edge.MIN_N と同じ値。ここを変えるなら向こうも変える。
_EDGE_TARGET_N = 300


def _update_status(date: str, index: dict, races: list[dict],
                   days: list[dict]) -> dict:
    """毎日の様子見用サマリを焼く（status.json）。

    毎朝コマンドを打つのは続かないので、**1 ページ見れば済む**形にする。
    ここは配信用のデータだけを作り、表示は frontend/status.html が受け持つ。

    **率（勝率・回収率）は載せない。** 検証中に毎日眺めると良い日・悪い日で
    判断が揺れる。#106 がまさにそれを避けるために基準を先に置いた。載せるのは
    「貯まり具合」と「壊れていないか」だけ。

    edge の n は日別ファイルを全部読まないと出せないので、ここで積み上げて
    おく。前日ぶんに今日ぶんを足す形にすれば、S3 の list を毎回舐めずに済む。
    """
    prev = _load_status()
    edge_rows = _count_edge_rows(races)
    n_edge = int(prev.get("edge", {}).get("n", 0)) + edge_rows

    # 直近 7 日の 1 日あたりペースから到達見込みを出す
    hist = (prev.get("edge", {}).get("history") or [])[-6:]
    hist.append({"date": date, "n": edge_rows})
    recent = [h["n"] for h in hist] or [0]
    per_day = sum(recent) / len(recent)
    remaining = max(0, _EDGE_TARGET_N - n_edge)
    eta_days = round(remaining / per_day) if per_day > 0 else None

    exotic = _count_exotic(races)
    doc = {
        "date": date,
        "days": len(days),
        "races_today": len(index.get("races") or []),
        # 検証の進捗。率は出さない（#106 の基準どおり）
        "edge": {"n": n_edge, "target": _EDGE_TARGET_N,
                 "remaining": remaining, "per_day": round(per_day, 1),
                 "eta_days": eta_days, "history": hist},
        # データの貯まり具合
        "coverage": {
            "records": _count_with(races, "records"),
            "results": _count_with(races, "result"),
            "exotic_races": exotic["races"],
            "exotic_pairs": exotic["pairs"],
        },
        # 今日どの馬を選んだか。**結果は載せない**（記録の表示に留める）
        "picks": _today_picks(races),
    }
    _put("status.json", doc, _CC_DAYS)
    return doc


def _load_status() -> dict:
    try:
        res = _get_s3().get_object(Bucket=os.environ["DATA_BUCKET"],
                                   Key="status.json")
        return json.loads(res["Body"].read())
    except Exception:
        return {}


def _count_with(races: list[dict], key: str) -> int:
    return sum(1 for r in races if r.get(key))


def _count_edge_rows(races: list[dict]) -> int:
    return sum(len(r.get("edges") or []) for r in races)


def _count_exotic(races: list[dict]) -> dict:
    n_races = n_pairs = 0
    for r in races:
        ex = r.get("exotic") or {}
        if not ex:
            continue
        n_races += 1
        n_pairs += sum(len(v or {}) for v in ex.values())
    return {"races": n_races, "pairs": n_pairs}


def _today_picks(races: list[dict]) -> list[dict]:
    """その日に閾値を超えた馬。**着順も的中も載せない。**

    「今日はこの馬を選んだ」という記録の表示に留める。結果を並べると
    毎日の当たり外れに引きずられ、判定まで待てなくなる。
    """
    out = []
    for r in races:
        for e in (r.get("edges") or []):
            out.append({
                "race_id": r.get("race_id"), "venue": r.get("venue"),
                "num": e.get("num"), "name": e.get("name"),
                "edge": e.get("edge"),
                "p_form": e.get("p_form"), "p_market": e.get("p_market"),
            })
    return sorted(out, key=lambda x: -(x.get("edge") or 0))
