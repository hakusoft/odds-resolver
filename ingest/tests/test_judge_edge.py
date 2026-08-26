"""二軸の乖離の判定（#117 Phase 3）。ネットワーク/AWS 非依存。

判定ロジックは先に決めた基準そのもの。ここが崩れると検証の意味が消える。
基準を変える PR は必ずこのテストを壊す。
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.tools.judge_edge import MIN_N, tally, verdict  # noqa: E402


def _row(won, odds=None, pos=1, top3=True):
    return {"won": won, "odds": odds, "pos": pos, "top3": top3}


def test_min_n_matches_106():
    """#106 と同じ基準にした。結果を並べて比べるため。"""
    assert MIN_N == 300


def test_tally_counts_payout_only_from_winners():
    rows = [_row(True, 5.0), _row(False, 20.0), _row(False, None)]
    t = tally(rows)
    assert t["n"] == 3 and t["wins"] == 1
    assert t["win_rate"] == pytest.approx(1 / 3)
    # 外れ馬の odds は payout に入れない（5.0 / 3 頭）
    assert t["payback"] == pytest.approx(5.0 / 3)


def test_tally_empty_does_not_divide_by_zero():
    t = tally([])
    assert t["n"] == 0 and t["payback"] == 0.0 and t["win_rate"] == 0.0


def test_top3_is_reference_only():
    """複勝は記録するが判定には使わない。両方見て良い方を採るのは後知恵。"""
    rows = [_row(False, 10.0, pos=2, top3=True)] * 3
    t = tally(rows)
    assert t["top3_rate"] == pytest.approx(1.0)
    # 複勝が満点でも単勝が 0 なら効果なし
    assert verdict({"n": MIN_N, "payback": t["payback"]}) == "効果なし"


def test_verdict_blocked_below_min_n():
    assert verdict({"n": MIN_N - 1, "payback": 9.9}) == "判定不可（n 不足）"


@pytest.mark.parametrize("payback,expected", [
    (1.50, "効果あり"),
    (1.0001, "効果あり"),
    (1.0, "効果なし"),      # ちょうど 100% は跨いだ扱い
    (0.9999, "効果なし"),
    (0.72, "効果なし"),      # #106 の実測値
])
def test_verdict_falls_to_no_effect_at_100pct(payback, expected):
    assert verdict({"n": MIN_N, "payback": payback}) == expected
