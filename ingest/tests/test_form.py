"""馬柱からの勝率推定（#117）。ネットワーク非依存。

この軸の生命線は**オッズを入力に取らない**こと。混ぜれば市場の写像になる。
特徴量を足す変更では、その値がオッズ由来でないかを必ず確認すること。
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.form import (  # noqa: E402
    MIN_RUNS, form_score, race_probabilities,
)


def _rec(num, runs):
    """runs は (着順, 頭数) のリスト。新しい順。"""
    return {"num": num,
            "recent": [{"pos": p, "field_size": f} for p, f in runs]}


def test_needs_minimum_runs():
    """近走が薄い馬は None。0.0 に潰すと新馬が最弱になる。"""
    assert form_score(_rec(1, [(1, 10), (1, 10)])) is None       # 2 走
    assert form_score(_rec(1, [(1, 10)] * MIN_RUNS)) is not None  # 3 走


def test_no_recent_at_all_is_none():
    assert form_score({"num": 1}) is None
    assert form_score({"num": 1, "recent": []}) is None


def test_runs_missing_field_size_are_dropped():
    """頭数が無い走は正規化できないので数えない。"""
    rec = {"num": 1, "recent": [
        {"pos": 1, "field_size": 10}, {"pos": 2, "field_size": 10},
        {"pos": 3},                      # field_size 欠け
    ]}
    assert form_score(rec) is None  # 使えるのが 2 走なので届かない


def test_stronger_horse_scores_higher():
    strong = form_score(_rec(1, [(1, 12), (1, 12), (2, 12)]))
    weak = form_score(_rec(2, [(11, 12), (12, 12), (10, 12)]))
    assert strong > weak


def test_field_size_is_normalized():
    """8 頭立ての 4 着と 16 頭立ての 4 着は同じではない。

    16 頭立ての 4 着の方が相対的に上位なので、高いスコアになる。
    """
    small = form_score(_rec(1, [(4, 8)] * 3))
    large = form_score(_rec(2, [(4, 16)] * 3))
    assert large > small


def test_score_bounded_0_to_1():
    best = form_score(_rec(1, [(1, 10)] * 5))
    worst = form_score(_rec(2, [(10, 10)] * 5))
    assert best == pytest.approx(1.0)
    assert worst == pytest.approx(0.0)


def test_recency_matters():
    """平均が同じでも、直近が良い方が高く出る（W_LAST の効果）。"""
    improving = form_score(_rec(1, [(1, 10), (5, 10), (9, 10)]))
    declining = form_score(_rec(2, [(9, 10), (5, 10), (1, 10)]))
    assert improving > declining


# --- レース単位の正規化（Phase 1-2） ---

def test_probabilities_sum_to_one():
    recs = [_rec(1, [(1, 8)] * 3), _rec(2, [(4, 8)] * 3), _rec(3, [(8, 8)] * 3)]
    out = race_probabilities(recs)
    assert sum(o["prob"] for o in out) == pytest.approx(1.0)


def test_probabilities_preserve_order():
    recs = [_rec(1, [(8, 8)] * 3), _rec(2, [(1, 8)] * 3)]
    out = {o["num"]: o["prob"] for o in race_probabilities(recs)}
    assert out[2] > out[1]


def test_unknown_horse_gets_average_weight():
    """推定不能な馬は 0 ではなく平均相当。新馬を「絶対勝たない」にしない。"""
    recs = [_rec(1, [(1, 8)] * 3), _rec(2, [(8, 8)] * 3), {"num": 3}]
    out = {o["num"]: o for o in race_probabilities(recs)}
    assert out[3]["score"] is None
    assert out[3]["prob"] > 0
    # 最強馬より低く、最弱馬より高い位置に収まる
    assert out[1]["prob"] > out[3]["prob"] > out[2]["prob"]
    assert sum(o["prob"] for o in out.values()) == pytest.approx(1.0)


def test_all_unknown_falls_back_to_uniform():
    recs = [{"num": 1}, {"num": 2}, {"num": 3}, {"num": 4}]
    out = race_probabilities(recs)
    assert all(o["prob"] == pytest.approx(0.25) for o in out)
    assert all(o["score"] is None for o in out)


def test_empty_race_does_not_crash():
    assert race_probabilities([]) == []


def test_odds_fields_are_ignored():
    """オッズ由来の値を混ぜても結果が変わらない（独立性の担保）。

    このテストが落ちたら、その変更は二軸の独立性を壊している。
    """
    plain = _rec(1, [(3, 10), (5, 10), (2, 10)])
    with_odds = dict(plain)
    with_odds["recent"] = [dict(r) for r in plain["recent"]]
    for r in with_odds["recent"]:
        r["popularity"] = 1      # 過去レースの市場評価
    with_odds["odds"] = 1.2      # 当該レースのオッズ
    with_odds["support"] = 0.85  # 支持率
    assert form_score(with_odds) == form_score(plain)
