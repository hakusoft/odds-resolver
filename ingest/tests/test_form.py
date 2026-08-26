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
    from ingest.form import MIN_SCORE
    best = form_score(_rec(1, [(1, 10)] * 5))
    worst = form_score(_rec(2, [(10, 10)] * 5))
    assert best == pytest.approx(1.0)
    # 全走最下位でも 0 にしない。0 だと「絶対に勝たない」ことになる
    assert worst == pytest.approx(MIN_SCORE)


def test_worst_horse_still_has_nonzero_probability():
    """素点 0 を許すと prob=0 になり乖離スコアが計算できない。

    実測では prob=0 の馬が 33 頭出て、うち 1 頭が実際に勝った（3.0%）。
    """
    from ingest.form import race_probabilities
    recs = [_rec(1, [(1, 10)] * 5), _rec(2, [(10, 10)] * 5)]
    out = race_probabilities(recs)
    assert all(o["prob"] > 0 for o in out)


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


# --- 二軸の交差（Phase 2） ---

def test_market_probabilities_sum_to_one():
    from ingest.form import market_probabilities
    p = market_probabilities([2.0, 4.0, 4.0])
    assert sum(x for x in p if x) == pytest.approx(1.0)
    # オッズが低いほど支持率が高い
    assert p[0] > p[1]


def test_market_probabilities_none_for_scratched():
    from ingest.form import market_probabilities
    p = market_probabilities([2.0, None, 0.0, 4.0])
    assert p[1] is None and p[2] is None
    assert sum(x for x in p if x) == pytest.approx(1.0)


def test_market_probabilities_all_missing():
    from ingest.form import market_probabilities
    assert market_probabilities([None, None]) == [None, None]
    assert market_probabilities([]) == []


def test_edge_sign_means_direction():
    """正 = 馬柱が市場より強く見ている（過小評価 = 買い候補）。"""
    from ingest.form import edge
    assert edge(0.30, 0.15) > 0    # 馬柱 30% vs 市場 15% → 買い
    assert edge(0.10, 0.20) < 0    # 馬柱 10% vs 市場 20% → 消し
    assert edge(0.20, 0.20) == pytest.approx(0.0)


def test_edge_is_symmetric_in_log_space():
    """対数比を選んだ理由。過小評価と過大評価が 0 を中心に対称になる。"""
    from ingest.form import edge
    assert edge(0.4, 0.2) == pytest.approx(-edge(0.2, 0.4))


def test_edge_treats_ratio_not_difference():
    """2%→4% と 20%→40% は同じ乖離として扱う（差分ではなく比）。"""
    from ingest.form import edge
    assert edge(0.04, 0.02) == pytest.approx(edge(0.40, 0.20))


def test_edge_none_when_market_missing():
    """支持率が無い馬は乖離を語れない。0 除算避けだけが理由ではない。"""
    from ingest.form import edge
    assert edge(0.2, None) is None
    assert edge(0.2, 0.0) is None
    assert edge(None, 0.2) is None
    assert edge(0.0, 0.2) is None


def test_race_edges_joins_both_axes():
    from ingest.form import race_edges
    recs = [_rec(1, [(1, 8)] * 3), _rec(2, [(8, 8)] * 3)]
    # 市場は 2 番を本命にしている（馬柱は 1 番が上）
    out = race_edges(recs, [10.0, 1.2])
    by = {o["num"]: o for o in out}
    assert by[1]["edge"] > 0   # 馬柱が強く見る馬 = 市場は軽視
    assert by[2]["edge"] < 0
    assert by[1]["p_form"] > by[1]["p_market"]


def test_race_edges_handles_scratched():
    from ingest.form import race_edges
    recs = [_rec(1, [(1, 8)] * 3), _rec(2, [(4, 8)] * 3)]
    out = race_edges(recs, [2.0, None])
    assert out[1]["edge"] is None
    assert out[0]["edge"] is not None


def test_race_edges_does_not_feed_odds_into_form():
    """独立性: オッズを変えても p_form は動かない。

    ここが二軸が初めて出会う場所。それ以前に混ざっていたら乖離が意味を失う。
    """
    from ingest.form import race_edges
    recs = [_rec(1, [(1, 8)] * 3), _rec(2, [(6, 8)] * 3)]
    a = race_edges(recs, [1.1, 50.0])
    b = race_edges(recs, [50.0, 1.1])
    assert [x["p_form"] for x in a] == [x["p_form"] for x in b]


def test_edge_threshold_is_two_sigma():
    """閾値は分布から決めた固定値。回収率を見る前に決めた（#117）。"""
    from ingest.form import EDGE_MEAN, EDGE_SD, EDGE_THRESHOLD
    assert EDGE_THRESHOLD == pytest.approx(EDGE_MEAN + 2.0 * EDGE_SD)
    assert EDGE_THRESHOLD == pytest.approx(3.105, abs=0.01)


def test_is_edge_pick_boundary():
    from ingest.form import EDGE_THRESHOLD, is_edge_pick
    assert is_edge_pick(EDGE_THRESHOLD) is True
    assert is_edge_pick(EDGE_THRESHOLD - 0.001) is False
    assert is_edge_pick(None) is False
    assert is_edge_pick(-5.0) is False
