"""組合せ馬券の歪み分布ツールのテスト（#56）。ネットワーク非依存。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.tools.exotic_edges import (  # noqa: E402
    _last_pre_post_odds, _parse_key, race_edges_exotic, summarize)


def test_parse_key_reads_pairs_and_trios():
    assert _parse_key("1-2") == (1, 2)
    assert _parse_key("1-2-3") == (1, 2, 3)


def test_parse_key_rejects_malformed():
    assert _parse_key("1") is None            # 1 頭は組でない
    assert _parse_key("1-2-3-4") is None      # 4 頭は扱わない
    assert _parse_key("1-1") is None          # 同じ馬番の重複
    assert _parse_key("a-b") is None


def _race(slots, exotic, n=4, post="15:00", signaled=None):
    """スナップショットと組合せオッズを持つ最小のレース。

    slots は (slot ラベル, オッズ列) のリスト。**time は post から逆算する**
    （slot "T-8" なら post の 8 分前）。実データでは time が真の基準で、
    slot はラベルに過ぎない（#169 で T-10 が欠けるレースがあると判明）。

    signaled に "HH:MM" を渡すと exotic_edges の signaled_at を作る
    （組合せを取得した時刻。これ以前の最後のスナップショットが選ばれる）。
    """
    import calendar
    h, m = (int(x) for x in post.split(":"))
    post_min = h * 60 + m

    def _hhmm(total):
        return f"{total // 60}:{total % 60:02d}"

    snaps = []
    for sl, o in slots:
        if sl.startswith("T-"):
            t = post_min - int(sl[2:])
        else:
            t = post_min          # F は発走時刻
        snaps.append({"slot": sl, "time": _hhmm(t), "odds": o})

    race = {
        "horses": [{"num": i} for i in range(1, n + 1)],
        "snapshots": snaps,
        "exotic": exotic,
        "post_time": post,
    }
    if signaled:
        sh, sm = (int(x) for x in signaled.split(":"))
        # JST の HH:MM を epoch に（_signaled_minutes が +9h して戻す）
        ts = calendar.timegm((2026, 10, 21, sh - 9, sm, 0, 0, 0, 0))
        race["exotic_edges"] = [{"kind": "wide", "signaled_at": ts}]
    return race


def test_last_pre_post_odds_uses_signaled_time():
    """**組合せを取得した時点で最新だったスナップショットを採る（#169）。**

    記録側（fetch）はその瞬間の最新を見る。signaled_at が残っているので
    それ以前の最後を採れば揃う。

    以前は「T-10 以内で最も遅いもの」を採っており、実測で 34/36 レースが
    T-2 になって記録と別の市場を見ていた（同じレースで共通 0 点）。
    """
    # 発走 15:00、取得 14:52（T-8 相当）。T-15 が最新で T-4 はまだ無い
    r = _race([("T-45", [1.0] * 4), ("T-15", [2.0] * 4), ("T-4", [3.0] * 4)],
              {}, signaled="14:52")
    odds, left = _last_pre_post_odds(r)
    assert odds == [2.0] * 4 and left == 15


def test_last_pre_post_odds_falls_back_to_post_time():
    """**signaled_at が無ければ発走時刻で切る。**

    閾値超えが 0 件のレースには signaled_at が無い。取得は発走直前まで
    走り得るので、発走までに存在した最後のスナップショットを採る。
    """
    r = _race([("T-15", [1.0] * 4), ("T-4", [2.0] * 4)], {})
    odds, left = _last_pre_post_odds(r)
    assert odds == [2.0] * 4 and left == 4


def test_last_pre_post_odds_ignores_midnight_crossing_signal():
    """**日付をまたぐ signaled_at は使わない（#169 のセルフレビュー）。**

    signaled_at は epoch なので JST の分に直すと日付が落ちる。発走 23:50 の
    記録が翌 00:05 なら 5 分と 1430 分の比較になり、記録より手前を選ぶ。

    実データでは 22:00 以降の発走が 0 件なので起きていないが、ナイターが
    延びれば起きうる。post を超えたら発走時刻で切る。
    """
    r = _race([("T-15", [1.0] * 4), ("T-4", [2.0] * 4)],
              {}, post="23:50", signaled="0:05")
    odds, left = _last_pre_post_odds(r)
    assert odds == [2.0] * 4 and left == 4      # 発走時刻で切った


def test_last_pre_post_odds_excludes_post_deadline():
    """発走後（F）は採らない。確定値だが『結果より先』の担保が崩れる。"""
    r = _race([("T-6", [1.0] * 4), ("F", [9.9] * 4)], {})
    odds, left = _last_pre_post_odds(r)
    assert odds == [1.0] * 4 and left == 6   # F ではなく T-6


def test_last_pre_post_odds_accepts_far_slots():
    """**遠いスロットでも採る（#169 で方針が変わった）。**

    以前は T-10 以内に限っていたが、T-10 が欠けるレースがある
    （#143・2 券種にするとほぼ 0%）。記録側はその瞬間の最新を見るので、
    T-45 しか無ければそれが使われる。ツールも同じにする。
    """
    r = _race([("T-45", [1.0] * 4), ("T-120", [3.0] * 4)], {})
    odds, left = _last_pre_post_odds(r)
    assert odds == [1.0] * 4 and left == 45


def test_last_pre_post_odds_drops_length_mismatch():
    """**オッズ列と馬番の長さが違えば捨てる（#169）。**

    S3 の snapshots は horses を持たないので、取消馬が出た回のオッズ列は
    horses より短くなる。zip は黙って短い方で止まり、**末尾の馬番が落ちて
    別の組を計算する**。記録側（fetch._latest_snapshot）と同じ防御。
    """
    r = _race([("T-8", [2.0, 4.0, 8.0])], {"umatan": {"1-2": 10.0}}, n=4)
    assert race_edges_exotic(r) == {}


def test_race_edges_exotic_computes_umatan():
    """単勝オッズから理論を作り、馬単オッズとの乖離を返す。"""
    r = _race([("T-8", [2.0, 4.0, 8.0, 8.0])],
              {"umatan": {"1-2": 10.0, "2-1": 12.0, "1-3": 20.0}})
    got = race_edges_exotic(r)
    assert "umatan" in got
    assert len(got["umatan"]) == 3
    assert all(isinstance(v, float) for v in got["umatan"])


def test_race_edges_exotic_skips_mislabeled_sanrenfuku():
    """**#137 の誤ラベルを読まない。**

    3 頭券種が 2 頭キーで保存されていた時期（8/26-8/28）のデータは、キーが
    指す組が実際とは違う。件数も値もそれらしいので、券種名とキーの幅の
    食い違いで弾く。ここを通すと歪みの分布が汚染される。
    """
    r = _race([("T-8", [2.0, 4.0, 8.0, 8.0])],
              {"sanrenfuku": {"1-2": 100.0, "1-3": 200.0}})   # 2 頭キー
    assert race_edges_exotic(r) == {}


def test_race_edges_exotic_accepts_proper_sanrenfuku():
    """3 頭キーの三連複は読む（#137 修正後の形）。"""
    r = _race([("T-8", [2.0, 4.0, 8.0, 8.0])],
              {"sanrenfuku": {"1-2-3": 30.0, "1-2-4": 40.0}})
    got = race_edges_exotic(r)
    assert "sanrenfuku" in got and len(got["sanrenfuku"]) == 2


def test_race_edges_exotic_needs_any_snapshot():
    """スナップショットが無ければ何も出さない（理論の入力が無い）。"""
    r = _race([], {"umatan": {"1-2": 10.0}})
    assert race_edges_exotic(r) == {}


def test_race_edges_exotic_no_exotic_is_empty():
    r = _race([("T-8", [2.0, 4.0, 8.0, 8.0])], {})
    assert race_edges_exotic(r) == {}


def test_summarize_reports_spread():
    s = summarize([-1.0, 0.0, 1.0])
    assert s["n"] == 3
    assert s["median"] == 0.0
    assert s["frac_positive"] == 1 / 3


def test_summarize_empty_is_none():
    assert summarize([]) is None
