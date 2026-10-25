"""検出力の見積もり（#171）のテスト。ネットワーク非依存。"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.tools.power import (  # noqa: E402
    binom_cdf, binom_pmf, binom_quantile, estimate, main)


def test_pmf_sums_to_one():
    assert sum(binom_pmf(k, 50, 0.1) for k in range(51)) == pytest.approx(1.0)


def test_cdf_and_quantile_agree():
    n, p = 300, 0.02
    k = binom_quantile(0.5, n, p)
    assert binom_cdf(k, n, p) >= 0.5
    assert binom_cdf(k - 1, n, p) < 0.5


def test_umatan_n300_was_unreadable():
    """**#56 の馬単を判定前に見積もっていれば分かったこと。**

    的中率 0.27% で n=300 は期待的中 0.81 件。4 割強の確率で 1 件も
    当たらず、回収率の区間は 0% から 300% 超まで開く。
    """
    r = estimate(300, 0.0027)
    assert r["expected_hits"] == pytest.approx(0.81)
    assert r["p_zero_hits"] > 0.4
    assert r["payback_lo"] == 0.0
    assert r["payback_hi"] > 3.0


def test_wide_n500_meets_expected_hits():
    """#170 の手計算（n=500 で期待的中 10.2 件）と一致する。"""
    r = estimate(500, 0.0204, min_hits=10)
    assert r["expected_hits"] == pytest.approx(10.2)
    # 期待値がちょうど基準の上なので、届かない確率は半分近く残る
    assert 0.3 < r["p_short_hits"] < 0.5


def test_interval_narrows_with_n():
    a, b = estimate(500, 0.02), estimate(50000, 0.02)
    assert (b["payback_hi"] - b["payback_lo"]) < (a["payback_hi"] - a["payback_lo"])


def test_false_positive_vanishes_with_large_n():
    """真の回収率 80% なら、n を積めば「効果あり」はほぼ出なくなる。"""
    assert estimate(100000, 0.02)["false_positive"] < 0.001


def test_rejects_degenerate_hit_rate():
    with pytest.raises(ValueError):
        estimate(300, 0.0)


def test_cli_prints_table(capsys):
    assert main(["--hit-rate", "0.0027", "--n", "300", "1000"]) == 0
    out = capsys.readouterr().out
    assert "300" in out and "1000" in out
    assert "下限" in out
