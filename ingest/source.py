"""取得元アダプタ。Crawl-Delay を厳守して 1 ページ取得する。"""
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
    return body


def day_list_path(date: str) -> str:
    # その日の開催一覧はトップに出る。日付指定はトップの明日/本日タブ相当を
    # 直接叩けないため、レース一覧の代表 RACEID を辿る起点としてトップを使う
    return "/"


def race_list_path(key: str) -> str:
    return f"/race_card/list/RACEID/{key}"


def odds_path(key: str) -> str:
    return f"/odds/tanfuku/RACEID/{key}"
