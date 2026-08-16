"""前向き検証の判定（#106）。ネットワーク/AWS 非依存。

判定ロジックは #106 で先に決めた基準そのものなので、ここが崩れると
検証の意味が消える。基準を変える PR は必ずこのテストを壊す。
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.tools.judge_forward import (  # noqa: E402
    MIN_N, select, tally, verdict,
)


def _row(support, won, odds=None, pos=1):
    return {"support": support, "won": won, "odds": odds, "pos": pos}


def test_min_n_is_300():
    """#106 で決めた基準。緩めるなら検証をやり直す。"""
    assert MIN_N == 300


def test_scope_band_takes_only_20_to_30():
    rows = [_row(0.19, False), _row(0.20, False), _row(0.25, False),
            _row(0.30, False), _row(0.50, False)]
    got = [r["support"] for r in select(rows, "band")]
    assert got == [0.20, 0.25]  # 下限は含み、上限は含まない


def test_scope_all_takes_everything():
    rows = [_row(0.01, False), _row(0.99, False)]
    assert len(select(rows, "all")) == 2


def test_unknown_scope_raises():
    with pytest.raises(ValueError):
        select([], "surged")


def test_tally_counts_payout_only_from_winners():
    rows = [_row(0.2, True, 3.0), _row(0.2, False, 9.9), _row(0.2, False, None)]
    t = tally(rows)
    assert t["n"] == 3
    assert t["wins"] == 1
    assert t["win_rate"] == pytest.approx(1 / 3)
    # 外れ馬の odds は payout に入れない（3.0 / 3 頭）
    assert t["payback"] == pytest.approx(1.0)


def test_tally_empty_does_not_divide_by_zero():
    t = tally([])
    assert t == {"n": 0, "wins": 0, "win_rate": 0.0, "payback": 0.0}


def test_verdict_blocked_below_min_n():
    t = {"n": MIN_N - 1, "payback": 5.0}  # 回収 500% でも判定しない
    assert verdict(t) == "判定不可（n 不足）"


@pytest.mark.parametrize("payback,expected", [
    (1.30, "効果あり"),
    (1.0001, "効果あり"),
    (1.0, "効果なし"),     # ちょうど 100% は「跨いだ」= 効果なしに倒す
    (0.9999, "効果なし"),
    (0.60, "効果なし"),
])
def test_verdict_falls_to_no_effect_at_100pct(payback, expected):
    """「100% を跨いだら効果なしに倒す」— 有意でないものを有りとしない。"""
    assert verdict({"n": MIN_N, "payback": payback}) == expected
