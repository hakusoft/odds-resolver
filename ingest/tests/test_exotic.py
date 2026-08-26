"""組合せ馬券の理論価格と歪み（#56）。ネットワーク非依存。"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.exotic import (  # noqa: E402
    edges, exacta_probabilities, market_from_odds, trio_probabilities,
)


def test_exacta_sums_to_one():
    p = {1: 0.5, 2: 0.3, 3: 0.2}
    e = exacta_probabilities(p)
    assert len(e) == 6                      # 3P2
    assert sum(e.values()) == pytest.approx(1.0)


def test_exacta_harville_formula():
    """P(i→j) = p_i * p_j / (1 - p_i)。手計算と一致すること。"""
    p = {1: 0.5, 2: 0.3, 3: 0.2}
    e = exacta_probabilities(p)
    assert e[(1, 2)] == pytest.approx(0.5 * 0.3 / 0.5)   # 0.30
    assert e[(2, 1)] == pytest.approx(0.3 * 0.5 / 0.7)   # 0.2143


def test_exacta_order_matters():
    """馬単は順序が効く。強い馬が先の方が高い。"""
    e = exacta_probabilities({1: 0.6, 2: 0.4})
    assert e[(1, 2)] > e[(2, 1)]


def test_exacta_skips_degenerate():
    """1 頭が確率 1.0 を占めると 2 着争いが定義できない。落とす。"""
    e = exacta_probabilities({1: 1.0, 2: 0.0})
    assert (1, 2) not in e


def test_trio_sums_to_one():
    t = trio_probabilities({1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1})
    assert len(t) == 4                      # 4C3
    assert sum(t.values()) == pytest.approx(1.0)


def test_trio_key_is_sorted():
    """順不同なのでキーは昇順に正規化する。"""
    t = trio_probabilities({3: 0.4, 1: 0.3, 2: 0.3})
    assert list(t) == [(1, 2, 3)]


def test_trio_stronger_combo_ranks_higher():
    t = trio_probabilities({1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1})
    assert t[(1, 2, 3)] > t[(2, 3, 4)]


def test_market_from_odds_normalizes():
    m = market_from_odds({(1, 2): 2.0, (2, 1): 4.0, (1, 3): 4.0})
    assert sum(m.values()) == pytest.approx(1.0)
    assert m[(1, 2)] > m[(2, 1)]            # 低オッズ = 高支持


def test_market_from_odds_drops_missing():
    """未発売・取消は分母にも入れない。残った組の中での相対支持率になる。"""
    m = market_from_odds({(1, 2): 2.0, (2, 1): None, (1, 3): 0.0})
    assert set(m) == {(1, 2)}
    assert m[(1, 2)] == pytest.approx(1.0)


def test_market_from_odds_all_missing():
    assert market_from_odds({(1, 2): None}) == {}
    assert market_from_odds({}) == {}


def test_edges_sign_means_direction():
    """正 = 理論が市場より高く見ている（過小評価 = 買い候補）。"""
    e = edges({(1, 2): 0.30}, {(1, 2): 0.10})
    assert e[(1, 2)] > 0
    e2 = edges({(1, 2): 0.05}, {(1, 2): 0.20})
    assert e2[(1, 2)] < 0


def test_edges_same_definition_as_win_axis():
    """#117 の単勝 edge と同じ対数比。単勝と組合せを同じものさしで比べる。"""
    from ingest.form import edge as win_edge
    e = edges({(1, 2): 0.3}, {(1, 2): 0.1})
    assert e[(1, 2)] == pytest.approx(win_edge(0.3, 0.1))


def test_edges_needs_both_sides():
    """片方に無い組は比べようがない。黙って 0 にしない。"""
    e = edges({(1, 2): 0.3, (1, 3): 0.2}, {(1, 2): 0.1})
    assert set(e) == {(1, 2)}


def test_exotic_path_builds_url():
    from ingest.source import exotic_path
    assert exotic_path("umatan", "202608261914060301") == \
        "/odds/umatan/RACEID/202608261914060301"


def test_exotic_path_rejects_unknown_kind():
    """券種名の打ち間違いを黙って通さない（存在しない URL を叩かない）。"""
    from ingest.source import exotic_path
    with pytest.raises(ValueError):
        exotic_path("tansho", "202608261914060301")
