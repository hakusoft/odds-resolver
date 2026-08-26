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


# --- 取得の選択ロジック（#56） ---

def _race(rid, post_time, **kw):
    return {"race_id": rid, "post_time": post_time,
            "source_key": "20260826" + rid[-4:], **kw}


def _at(hhmm, date="20260826"):
    """JST の HH:MM を epoch に。"""
    import calendar
    h, m = (int(x) for x in hhmm.split(":"))
    y, mo, d = int(date[:4]), int(date[4:6]), int(date[6:8])
    return calendar.timegm((y, mo, d, h - 9, m, 0, 0, 0, 0))


def _fetch_mod(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import fetch
    importlib.reload(fetch)
    return fetch


def test_pick_exotic_only_near_post(monkeypatch):
    """締切間際だけ取る。早い時間帯は市場が固まっていない。"""
    f = _fetch_mod(monkeypatch)
    races = [_race("20260826-oi-01", "15:00")]
    assert f._pick_exotic(_at("14:00"), races) is None   # 60 分前
    assert f._pick_exotic(_at("14:55"), races) is not None  # 5 分前


def test_pick_exotic_skips_started_race(monkeypatch):
    """発走済みは取らない。確定値だが『結果より先』の担保が崩れる。"""
    f = _fetch_mod(monkeypatch)
    races = [_race("20260826-oi-01", "15:00")]
    assert f._pick_exotic(_at("15:01"), races) is None


def test_pick_exotic_walks_kinds(monkeypatch):
    """1 レース 1 券種 1 回。取得済みは飛ばして次の券種へ。"""
    f = _fetch_mod(monkeypatch)
    now = _at("14:55")
    races = [_race("20260826-oi-01", "15:00")]
    _, first = f._pick_exotic(now, races)
    assert first == f.EXOTIC_KINDS[0]

    races[0]["exotic_done"] = [f.EXOTIC_KINDS[0]]
    _, second = f._pick_exotic(now, races)
    assert second == f.EXOTIC_KINDS[1]

    races[0]["exotic_done"] = list(f.EXOTIC_KINDS)
    assert f._pick_exotic(now, races) is None


def test_pick_exotic_needs_source_key(monkeypatch):
    f = _fetch_mod(monkeypatch)
    races = [{"race_id": "20260826-oi-01", "post_time": "15:00"}]
    assert f._pick_exotic(_at("14:55"), races) is None


def test_exotic_slot_matches_edge_slot(monkeypatch):
    """単勝の乖離と同じ時点で取る。ずらすと同じ瞬間の比較ができない。"""
    f = _fetch_mod(monkeypatch)
    assert f.EXOTIC_SLOT_MINUTES == f.EDGE_SLOT_MINUTES


def test_api_exposes_exotic(monkeypatch):
    """組合せオッズが S3 view に焼かれる経路（api の整形を archive が共用）。

    DynamoDB は TTL 2 日なので、焼かないと消える。
    """
    from decimal import Decimal
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import api
    importlib.reload(api)

    meta = {"race_id": "20260826-oi-01", "venue": "大井", "race_no": Decimal(1),
            "post_time": "15:00", "name": "テスト", "n_horses": Decimal(8),
            "surface": "ダ", "distance": Decimal(1200),
            "exotic": {"umatan": {"1-2": Decimal("12.3")}}}

    def fake_query(pk, **kw):
        return [meta] if pk.startswith("DAY#") else []

    monkeypatch.setattr(api, "_query", fake_query)
    out = api._race("20260826-oi-01")
    assert out["exotic"]["umatan"]["1-2"] == 12.3


def test_api_omits_exotic_when_absent(monkeypatch):
    """取得前のレースには exotic キーを生やさない（欠測を空扱いにしない）。"""
    from decimal import Decimal
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import api
    importlib.reload(api)

    meta = {"race_id": "20260826-oi-01", "venue": "大井", "race_no": Decimal(1),
            "post_time": "15:00", "name": "テスト", "n_horses": Decimal(8),
            "surface": "ダ", "distance": Decimal(1200)}
    monkeypatch.setattr(api, "_query",
                        lambda pk, **kw: [meta] if pk.startswith("DAY#") else [])
    assert "exotic" not in api._race("20260826-oi-01")
