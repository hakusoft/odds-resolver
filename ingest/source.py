"""取得元アダプタ。Crawl-Delay を厳守して 1 ページ取得する。

取得した本文は原本として S3 に残す（#172）。パーサのバグで過去データが
戻らない状態をやめるため（#112 では原本が無く 1532 頭ぶん再パースできなかった）。
"""
import gzip
import os
import sys
import time
import urllib.request

BASE = "https://keiba.rakuten.co.jp"
CRAWL_DELAY_SEC = 60  # robots.txt の規定。絶対制約
_UA = "OddsResolverBot/1.0 (+https://github.com/hakusoft/odds-resolver)"
_last_fetch = 0.0


def fetch(path: str) -> str:
    global _last_fetch
    wait = CRAWL_DELAY_SEC - (time.monotonic() - _last_fetch)
    if _last_fetch and wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(BASE + path, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as res:
        body = res.read().decode("utf-8", errors="replace")
    _last_fetch = time.monotonic()
    _save_raw(path, body)
    return body


_s3 = None


def raw_key(path: str, ts: float) -> str:
    """原本の置き場所。JST の日付で切り、時刻で一意にする（append-only）。

    同じパスは Crawl-Delay で 60 秒以上空くので、秒までで衝突しない。
    """
    jst = time.gmtime(ts + 9 * 3600)
    slug = path.strip("/").replace("/", "_") or "top"
    return (f"html/{time.strftime('%Y%m%d', jst)}/{slug}/"
            f"{time.strftime('%H%M%S', jst)}.html.gz")


def _save_raw(path: str, body: str):
    """原本を S3 に置く。**失敗しても取得は止めない。**

    取得は Crawl-Delay の 1 分枠で動いており、保存の失敗でオッズを
    取り損なうほうが損失が大きい。RAW_BUCKET が無ければ何もしない
    （テスト・手元実行で S3 に触らない）。
    """
    global _s3
    bucket = os.environ.get("RAW_BUCKET")
    if not bucket:
        return
    try:
        if _s3 is None:
            import boto3
            _s3 = boto3.client("s3")
        _s3.put_object(
            Bucket=bucket, Key=raw_key(path, time.time()),
            Body=gzip.compress(body.encode("utf-8")),
            ContentType="text/html; charset=utf-8", ContentEncoding="gzip")
    except Exception as e:
        print(f"raw save failed: {path}: {e!r}", file=sys.stderr)


def day_list_path(date: str) -> str:
    # その日の開催一覧はトップに出る。日付指定はトップの明日/本日タブ相当を
    # 直接叩けないため、レース一覧の代表 RACEID を辿る起点としてトップを使う
    return "/"


def race_list_path(key: str) -> str:
    return f"/race_card/list/RACEID/{key}"


def odds_path(key: str) -> str:
    return f"/odds/tanfuku/RACEID/{key}"


def result_path(key: str) -> str:
    return f"/race_performance/list/RACEID/{key}"


def record_path(key: str) -> str:
    # 馬柱は各レースの出馬表ページに載る（race_list と同じ URL 形だが、
    # 会場代表ではなく各レースの key を渡す点が異なる・#55）
    return f"/race_card/list/RACEID/{key}"


# 組合せ馬券のオッズ（#56）。単複と同じ RACEID で券種ごとにページが分かれる。
# 実測では三連単ですら 1 ページに全点が載る（分割ページは無い）。
_EXOTIC_KINDS = ("umafuku", "umatan", "wide", "sanrenfuku", "sanrentan")


def exotic_path(kind: str, key: str) -> str:
    """券種別オッズのパス。kind は _EXOTIC_KINDS のいずれか。"""
    if kind not in _EXOTIC_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return f"/odds/{kind}/RACEID/{key}"
