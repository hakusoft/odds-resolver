"""頭数別較正の判定ツールのテスト（#148）。ネットワーク非依存。

**基準を固定するのがこのテストの仕事。** MIN_N と「3 ポイント未満なら
効果なし」を書き換えたら落ちる。判定日に緩めるのを防ぐため。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.metrics import CALIB_BINS  # noqa: E402
from ingest.tools.judge_field_size import (  # noqa: E402
    DIFF_POINTS, EXCLUDED_BINS, GROUPS, MIN_N, compare, readiness, verdict)

NBINS = len(CALIB_BINS) - 1


def _rows(n=MIN_N, wins=0):
    return [{"n": n, "wins": wins, "payback": 0.0, "sum_support": 0.0}
            for _ in range(NBINS)]


def _per(b_rows, c_rows):
    return {"B": b_rows, "C": c_rows}


def test_min_n_is_200():
    """#148 で先に決めた基準。緩めるなら検証をやり直す。"""
    assert MIN_N == 200


def test_diff_threshold_is_3_points():
    """同じ帯で 3 ポイント以上離れていなければ「違いは無い」に倒す。"""
    assert DIFF_POINTS == 3.0


def test_group_boundaries_are_fixed():
    """区分の境界は検証期間中に動かさない（#148）。"""
    assert GROUPS == {"B": (8, 9), "C": (10, 11)}


def test_readiness_blocks_when_any_band_thin():
    """**律速帯だけ見て足りたことにしない。** 全 7 帯が n>=200 で判定可能。"""
    thin = _rows()
    thin[2] = {"n": MIN_N - 1, "wins": 0, "payback": 0.0, "sum_support": 0.0}
    r = readiness(_per(thin, _rows()))
    assert r["B"]["ready"] is False
    assert r["B"]["thin_bins"] == 1
    assert r["ready"] is False


def test_readiness_remaining_tracks_the_thinnest_band():
    """**残りは最小の帯から出す。** 最後の帯を決め打ちにしない。

    `0.50-1.00` が律速なのは実測の性質（#147）であって保証ではない。
    決め打ちだと、別の帯が薄い時に「remaining 0 なのに ready false」と
    いう読めない出力になる。判定日にここを読み違えると困る。
    """
    rows = _rows(n=1000)
    rows[2] = {"n": 150, "wins": 0, "payback": 0.0, "sum_support": 0.0}
    r = readiness(_per(rows, _rows(n=1000)))
    assert r["B"]["remaining"] == 50            # 1000 ではなく 150 から出す
    assert r["B"]["limiting_band"] == "0.10-0.15"
    assert r["B"]["ready"] is False


def test_readiness_ready_when_all_bands_full():
    r = readiness(_per(_rows(), _rows()))
    assert r["ready"] is True
    assert r["B"]["remaining"] == 0


def test_readiness_reports_no_rates():
    """--check は率を出さない。数字を見てから基準を動かす隙を作らない。"""
    r = readiness(_per(_rows(n=MIN_N, wins=123), _rows(n=MIN_N, wins=7)))
    blob = repr(r)
    assert "win_rate" not in blob and "payback" not in blob
    assert "wins" not in blob


def test_verdict_no_difference_when_under_threshold():
    """差が 3 ポイント未満なら「違いは無い」。有意でないものを有りとしない。"""
    b = _rows(n=1000, wins=200)          # 20.0%
    c = _rows(n=1000, wins=225)          # 22.5% → 差 2.5pt
    v = verdict(compare(_per(b, c)))
    assert v["verdict"] == "頭数による違いは無い"
    assert v["bands_over_threshold"] == []


def test_verdict_difference_when_at_threshold():
    """ちょうど 3 ポイントは「違いがある」側（>= で判定）。"""
    b = _rows(n=1000, wins=200)          # 20.0%
    c = _rows(n=1000, wins=230)          # 23.0% → 差 3.0pt
    v = verdict(compare(_per(b, c)))
    assert v["verdict"] == "頭数による違いがある"
    assert len(v["bands_over_threshold"]) == NBINS - len(EXCLUDED_BINS)


def test_verdict_ignores_excluded_band():
    """0.00-0.05 は横軸が揃っていないので差を読まない（#148）。

    この帯だけ大きく離れていても「違いは無い」に倒れる。
    """
    b, c = _rows(n=1000, wins=200), _rows(n=1000, wins=200)
    for i in EXCLUDED_BINS:
        c[i] = {"n": 1000, "wins": 900, "payback": 0.0, "sum_support": 0.0}
    rows = compare(_per(b, c))
    assert any(r["excluded"] for r in rows)
    v = verdict(rows)
    assert v["verdict"] == "頭数による違いは無い"


def test_excluded_band_still_counts_for_readiness():
    """除外するのは差を読む対象からで、充足判定からではない（#148）。"""
    b = _rows()
    for i in EXCLUDED_BINS:
        b[i] = {"n": MIN_N - 1, "wins": 0, "payback": 0.0, "sum_support": 0.0}
    r = readiness(_per(b, _rows()))
    assert r["B"]["ready"] is False      # 除外帯でも n 不足なら判定不可


def test_compare_keeps_all_bands_visible():
    """除外帯も行としては残す。隠すと後から「見ていない」と分からなくなる。"""
    rows = compare(_per(_rows(n=1000, wins=100), _rows(n=1000, wins=100)))
    assert len(rows) == NBINS
    assert [r["band"] for r in rows][0] == "0.00-0.05"


def test_compare_diff_sign_is_b_minus_c():
    """差の符号は B - C。読み違えると解釈が逆になる。"""
    b = _rows(n=1000, wins=300)          # 30%
    c = _rows(n=1000, wins=200)          # 20%
    rows = compare(_per(b, c))
    assert rows[1]["diff_points"] == 10.0
