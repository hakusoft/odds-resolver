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
from .metrics import CALIB_BINS, calibration_bins, classify_race
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
    classes = []  # 当日総括（#83）用の分類。追加クエリなしで同ループから
    for r in index["races"]:
        race = _race(r["race_id"])
        _put(f"races/{r['race_id']}.json", race, _CC_RACE)
        if _accumulate_calib(day_calib, race):
            n_scored += 1
        c = classify_race(race)
        if c is not None:
            classes.append({"race_id": r["race_id"], "venue": r["venue"],
                            "race_no": r["race_no"], **c})
    index["summary"] = _summarize(classes)
    _put(f"{date}/index.json", index, _CC_DAY_INDEX)

    days = _update_days(index)
    _update_calibration(date, day_calib, n_scored)
    return {"date": date, "races": len(index["races"]), "days": len(days),
            "calib_races": n_scored}


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


def _empty_bins() -> list[dict]:
    return [{"n": 0, "sum_support": 0.0, "wins": 0, "payback": 0.0}
            for _ in range(len(CALIB_BINS) - 1)]


def _empty_calib_set() -> dict:
    return {k: _empty_bins() for k in _CALIB_SETS}


def _accumulate_calib(acc: dict, race: dict) -> bool:
    """レース詳細から全馬/急変あり/急変なしの較正を acc に足す。

    着順・オッズが無ければ False。急変判定は surge.py を全スナップ
    ショットに適用（可視化 #73 と同じ定義）。
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
    computed = {}
    for k in _CALIB_SETS:
        bins = calibration_bins(odds, winner_idx, masks[k])
        if bins is None:
            return False
        computed[k] = bins
    for k in _CALIB_SETS:
        for a, b in zip(acc[k], computed[k]):
            a["n"] += b["n"]
            a["sum_support"] += b["sum_support"]
            a["wins"] += b["wins"]
            a["payback"] += b["payback"]
    return True


def _update_calibration(date: str, day_calib: dict, n_races: int):
    doc = _load_calibration()
    # 再焼きで同日を上書きしても二重計上しないよう、日別に置き換える
    doc["by_date"][date] = {"races": n_races, "sets": day_calib}
    total = _empty_calib_set()
    for entry in doc["by_date"].values():
        sets = entry.get("sets") or {"total": entry.get("bins", _empty_bins())}
        for k in _CALIB_SETS:
            for t, b in zip(total[k], sets.get(k, _empty_bins())):
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
    # 集計値だけを持ち生レースを残さないので、この機能より前に焼いた日
    # （7/25〜7/31）は新キーが 0 のまま埋まらない。遡って埋めるには当該日を
    # 再焼き（archive を date 指定で再実行）する必要がある。読む側が誤解
    # しないよう since に開始日を出す。
    # 新軸を実際に持つ最初の日。この日より前は 0 なので、by_persistence /
    # by_timing の分母は total と揃わない。読む側はここを見て期間を合わせる。
    detailed = sorted(d for d, e in doc["by_date"].items()
                      if "persist" in (e.get("sets") or {}))
    since = detailed[0] if detailed else None
    doc["by_persistence"] = {"persist": _finalize_bins(total["persist"]),
                             "revert": _finalize_bins(total["revert"]),
                             "since": since}
    doc["by_timing"] = {"late": _finalize_bins(total["late"]),
                        "early": _finalize_bins(total["early"]),
                        "since": since}
    doc["surge_threshold"] = {"min_slot": SURGE_MIN_SLOT, "delta": SURGE_DELTA,
                              "late_window": LATE_WINDOW}
    doc["n_days"] = len(doc["by_date"])
    doc["n_races"] = sum(e["races"] for e in doc["by_date"].values())
    # 毎日更新される累積ファイルなので目次類と同じ短キャッシュにする
    _put("calibration.json", doc, _CC_DAYS)


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
