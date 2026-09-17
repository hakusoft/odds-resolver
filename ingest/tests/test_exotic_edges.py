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


def _race(slots, exotic, n=4):
    """スナップショットと組合せオッズを持つ最小のレース。"""
    return {
        "horses": [{"num": i} for i in range(1, n + 1)],
        "snapshots": [{"slot": s, "odds": o} for s, o in slots],
        "exotic": exotic,
    }


def test_last_pre_post_odds_picks_latest_within_window():
    """締切前で最も遅いスロットを採る。組合せの取得時点に寄せるため。"""
    r = _race([("T-45", [1.0] * 4), ("T-8", [2.0] * 4), ("T-20", [3.0] * 4)], {})
    odds, m = _last_pre_post_odds(r)
    assert m == 8 and odds == [2.0] * 4


def test_last_pre_post_odds_excludes_post_deadline():
    """発走後（F）は採らない。確定値だが『結果より先』の担保が崩れる。"""
    r = _race([("T-6", [1.0] * 4), ("F", [9.9] * 4)], {})
    odds, m = _last_pre_post_odds(r)
    assert m == 6 and odds == [1.0] * 4      # F ではなく T-6 を採る


def test_last_pre_post_odds_ignores_far_slots():
    """T-10 より前のスロットしか無ければ使わない（単勝と時点がずれる）。"""
    r = _race([("T-45", [1.0] * 4), ("T-120", [1.0] * 4)], {})
    odds, m = _last_pre_post_odds(r)
    assert odds is None and m is None


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


def test_race_edges_exotic_needs_pre_post_snapshot():
    """締切前の単勝が無ければ何も出さない（理論の入力が無い）。"""
    r = _race([("T-45", [2.0, 4.0, 8.0, 8.0])], {"umatan": {"1-2": 10.0}})
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
