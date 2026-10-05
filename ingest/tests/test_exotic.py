"""組合せ馬券の理論価格と歪み（#56）。ネットワーク非依存。"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.exotic import (  # noqa: E402
    edges, exacta_probabilities, exotic_threshold, is_exotic_edge_pick,
    is_exotic_edge_pick_for, market_from_odds, quinella_probabilities,
    theory_for, trio_probabilities, wide_probabilities,
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


# --- ワイド（#160 / #56 の次の測定先）---------------------------------------

def _wide_direct(probs, a, b):
    """Harville の順列から直接。**畳み込みとは独立な経路で検算するため。**

    `wide_probabilities` は三連複を畳んで出しているので、同じ関数を使って
    検証すると実装の誤りを一緒に見落とす（#137 の教訓: 点数が合っていても
    正しさの証拠にならない）。ここでは 1-3 着の順列を全部回して数える。
    """
    from itertools import permutations
    tot = 0.0
    for i, j, k in permutations(probs, 3):
        if a not in (i, j, k) or b not in (i, j, k):
            continue
        pi, pj, pk = probs[i], probs[j], probs[k]
        r1 = 1.0 - pi
        r2 = r1 - pj
        if r1 <= 0 or r2 <= 0:
            continue
        tot += pi * (pj / r1) * (pk / r2)
    return tot


def test_wide_matches_direct_enumeration():
    """**畳み込みが素朴な定義と一致すること。** 独立な経路で検算する。"""
    p = {1: 0.35, 2: 0.25, 3: 0.20, 4: 0.12, 5: 0.08}
    w = wide_probabilities(p)
    for (a, b), got in w.items():
        assert got == pytest.approx(_wide_direct(p, a, b)), f"{a}-{b}"


def test_wide_sums_to_three_not_one():
    """**ワイドの合計は 3.0。** 馬単・三連複（1.0）と性質が違う。

    3 着以内の 3 頭からペアが 3 組できるので、毎レース必ず 3 組当たる。
    market_from_odds は合計 1.0 に正規化するので、edges で比べると
    log(3) のゲタが全組に乗る。**馬単の閾値を流用できない根拠。**
    """
    p = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1}
    w = wide_probabilities(p)
    assert len(w) == 6                      # 4C2
    assert sum(w.values()) == pytest.approx(3.0)


def test_wide_key_is_sorted():
    """順不同なのでキーは昇順に正規化する（三連複と同じ）。"""
    w = wide_probabilities({4: 0.4, 1: 0.3, 2: 0.2, 3: 0.1})
    assert all(k == tuple(sorted(k)) for k in w)
    assert (1, 4) in w and (4, 1) not in w


def test_wide_degenerates_at_three_horses():
    """**3 頭立てでは全組が確率 1.0。** 3 頭しかいなければ全員 3 着以内。

    理論 1.0 に対し市場は控除率ぶん 1.0 未満なので edge が必ず正に出て、
    **歪みではなく頭数の少なさを拾う**。この退化があるため `theory_for`
    は 3 頭立てのワイドを計算しない（下の門のテスト）。

    ここは素の関数の性質を記録しておくためのテスト。
    """
    w = wide_probabilities({1: 0.5, 2: 0.3, 3: 0.2})
    assert len(w) == 3                                  # 3C2
    assert all(v == pytest.approx(1.0) for v in w.values())


def test_wide_is_normal_from_four_horses():
    """4 頭立てなら退化しない（均等配分で 0.5）。門を 4 に置いた根拠。"""
    w = wide_probabilities({1: 0.25, 2: 0.25, 3: 0.25, 4: 0.25})
    assert all(v == pytest.approx(0.5) for v in w.values())


def test_wide_probability_never_exceeds_one():
    """個々の組の確率は 1.0 を超えない（合計が 3.0 でも各項は確率）。"""
    p = {1: 0.5, 2: 0.25, 3: 0.15, 4: 0.06, 5: 0.04}
    for k, v in wide_probabilities(p).items():
        assert 0.0 <= v <= 1.0, f"{k}={v}"


def test_wide_stronger_pair_ranks_higher():
    w = wide_probabilities({1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1})
    assert w[(1, 2)] > w[(3, 4)]


def test_wide_beats_exacta_on_same_pair():
    """**ワイドの方が当たりやすい。** 次の測定先に選んだ理由そのもの。

    同じ 2 頭でも「3 着以内に 2 頭」は「1-2 着を順番どおり」より緩い。
    #56 の判定（馬単 n=367 で的中 1 件）で検出力不足が分かったので、
    的中率の高い帯を測りたい。
    """
    p = {1: 0.35, 2: 0.25, 3: 0.20, 4: 0.12, 5: 0.08}
    w = wide_probabilities(p)
    e = exacta_probabilities(p)
    assert w[(1, 2)] > e[(1, 2)] + e[(2, 1)]


def test_quinella_folds_exacta_both_ways():
    """馬複は馬単の両順の和。合計は 1.0 のまま。"""
    p = {1: 0.5, 2: 0.3, 3: 0.2}
    q = quinella_probabilities(p)
    e = exacta_probabilities(p)
    assert q[(1, 2)] == pytest.approx(e[(1, 2)] + e[(2, 1)])
    assert sum(q.values()) == pytest.approx(1.0)
    assert len(q) == 3                      # 3C2


# --- 券種ごとの理論式の振り分け（#160）------------------------------------

def test_theory_for_picks_by_kind_not_width():
    """**幅ではなく券種で選ぶ。** これを間違えたのが #160 の発端。

    wide と umafuku は馬単と同じ 2 頭キーだが、当たりの定義が違うので
    理論式も違う。幅で分岐すると両方 exacta になる。
    """
    p = {1: 0.35, 2: 0.25, 3: 0.20, 4: 0.12, 5: 0.08}
    assert theory_for("umatan", p) == exacta_probabilities(p)
    assert theory_for("umafuku", p) == quinella_probabilities(p)
    assert theory_for("wide", p) == wide_probabilities(p)
    assert theory_for("sanrenfuku", p) == trio_probabilities(p)


def test_theory_for_wide_differs_from_exacta():
    """ワイドを exacta で計算すると 4〜8 倍ずれる。取り違えを固定で防ぐ。"""
    p = {1: 0.35, 2: 0.25, 3: 0.20, 4: 0.12, 5: 0.08}
    w = theory_for("wide", p)
    e = theory_for("umatan", p)
    assert w[(1, 2)] > e[(1, 2)] * 3        # 実測 4.4 倍


def test_theory_for_returns_none_without_formula():
    """三連単は理論式が無い。順不同で代用せず None を返す。"""
    p = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1}
    assert theory_for("sanrentan", p) is None
    assert theory_for("tansho", p) is None


def test_theory_for_refuses_three_horse_wide():
    """**3 頭立てのワイドは計算しない。** 退化して歪みを測れない。

    全組が確率 1.0 になるので edge が必ず正に出る。「市場が安い」のでは
    なく「3 頭しかいない」ことを拾っているだけ。4 頭からは成立する。
    """
    p3 = {1: 0.5, 2: 0.3, 3: 0.2}
    assert theory_for("wide", p3) is None
    assert theory_for("sanrenfuku", p3) is None     # 組が 1 つで比較に意味が無い

    p4 = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1}
    assert theory_for("wide", p4) is not None
    assert theory_for("sanrenfuku", p4) is not None


def test_theory_for_two_horse_kinds_still_work():
    """馬単・馬複は 2 頭で成立する（門はワイドと三連複だけ）。"""
    p2 = {1: 0.6, 2: 0.4}
    assert theory_for("umatan", p2) is not None
    assert theory_for("umafuku", p2) is not None
    assert theory_for("wide", p2) is None


# --- 券種ごとの閾値（#160）-------------------------------------------------

def test_threshold_exists_only_for_measured_kind():
    """**測っていない券種の閾値は None。** 馬単だけが測ってある。"""
    assert exotic_threshold("umatan") == pytest.approx(-0.560 + 2 * 0.928)
    assert exotic_threshold("wide") is None
    assert exotic_threshold("sanrenfuku") is None


def test_pick_for_never_fires_without_threshold():
    """**閾値が無い券種は記録しない。** どれだけ大きい edge でも False。

    記録してから閾値を決めると、分布ではなく回収率を見て決められて
    しまう（#56 の「先に決めた基準を動かさない」が崩れる）。
    オッズの取得は続くので分布は後から出せる。
    """
    assert is_exotic_edge_pick_for("wide", 99.0) is False
    assert is_exotic_edge_pick_for("wide", 1.5) is False
    assert is_exotic_edge_pick_for("sanrentan", 99.0) is False


def test_pick_for_uses_the_kinds_threshold():
    """馬単は既存の閾値で判定する（挙動を変えない）。"""
    t = exotic_threshold("umatan")
    assert is_exotic_edge_pick_for("umatan", t) is True
    assert is_exotic_edge_pick_for("umatan", t - 0.001) is False
    assert is_exotic_edge_pick_for("umatan", None) is False


def test_pick_for_matches_legacy_on_umatan():
    """**既存の is_exotic_edge_pick と馬単では一致する。**

    #56 の判定に使った経路なので、ここがずれると過去の記録と
    比較できなくなる。
    """
    for e in (-2.0, 0.0, 1.29, 1.295, 1.296, 2.0, 5.0):
        assert is_exotic_edge_pick_for("umatan", e) == is_exotic_edge_pick(e)


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
    """1 レース 1 券種 1 回。取得済みは飛ばして次の券種へ。

    券種の本数に依存しない書き方にしてある（#137 で三連複を外し
    EXOTIC_KINDS が 1 本になったため）。見たいのは「未取得を順に返し、
    尽きたら None」であって、何本あるかではない。
    """
    f = _fetch_mod(monkeypatch)
    now = _at("14:55")
    races = [_race("20260826-oi-01", "15:00")]

    seen = []
    for _ in f.EXOTIC_KINDS:
        picked = f._pick_exotic(now, races)
        assert picked is not None
        seen.append(picked[1])
        races[0]["exotic_done"] = list(seen)

    assert seen == list(f.EXOTIC_KINDS)   # 定義順に、重複なく返る
    assert f._pick_exotic(now, races) is None   # 尽きたら止まる


def test_pick_exotic_needs_source_key(monkeypatch):
    f = _fetch_mod(monkeypatch)
    races = [{"race_id": "20260826-oi-01", "post_time": "15:00"}]
    assert f._pick_exotic(_at("14:55"), races) is None


def test_exotic_slot_matches_edge_slot(monkeypatch):
    """単勝の乖離と同じ時点で取る。ずらすと同じ瞬間の比較ができない。"""
    f = _fetch_mod(monkeypatch)
    assert f.EXOTIC_SLOT_MINUTES == f.EDGE_SLOT_MINUTES


# 点数の突き合わせ（#137）
def test_shortfall_none_when_complete(monkeypatch):
    """全組揃っていれば報告しない。"""
    from itertools import combinations, permutations
    f = _fetch_mod(monkeypatch)
    trio = {c: 1.5 for c in combinations(range(1, 9), 3)}      # 8C3 = 56
    assert f._exotic_shortfall("sanrenfuku", trio) is None
    tan = {p: 1.5 for p in permutations(range(1, 9), 3)}       # 8P3 = 336
    assert f._exotic_shortfall("sanrentan", tan) is None
    pair = {p: 1.5 for p in permutations(range(1, 9), 2)}      # 8*7 = 56
    assert f._exotic_shortfall("umatan", pair) is None


def test_shortfall_catches_the_137_mislabel(monkeypatch):
    """#137 の誤ラベルはこの検査で落ちる。

    14 頭立ての三連複で期待 364 点に対し 53 点しか保存されていなかった。
    件数だけ見ても気づけなかったのが #137 の教訓なので、期待値との
    突き合わせを機械で回す。
    """
    f = _fetch_mod(monkeypatch)
    # 2 頭キーで 53 件保存された当時の形（キーの馬番は 14 頭ぶん現れる）
    pairs = [(x, y) for x in range(1, 15) for y in range(1, 15) if x != y]
    got = {p: 1.5 for p in pairs[:53]}
    short = f._exotic_shortfall("sanrenfuku", got)
    assert short is not None
    assert short["expect"] == 364      # 14C3
    assert short["got"] == 53


def test_shortfall_counts_n_from_keys_not_max(monkeypatch):
    """n は馬番の種類数で数える。最大値だと取消馬が居る時に過大になる。

    馬番 1,2,3,5 の 4 頭立て（4 番が取消）なら期待は 4C3 = 4。
    最大値 5 を n とすると 5C3 = 10 になり、正常な取得を欠測と誤る。
    """
    from itertools import combinations
    f = _fetch_mod(monkeypatch)
    trio = {c: 1.5 for c in combinations([1, 2, 3, 5], 3)}
    assert f._exotic_shortfall("sanrenfuku", trio) is None


def test_shortfall_ignores_empty(monkeypatch):
    """取得できなかった場合は shortfall ではなく欠測。区別する。"""
    f = _fetch_mod(monkeypatch)
    assert f._exotic_shortfall("sanrenfuku", None) is None
    assert f._exotic_shortfall("sanrenfuku", {}) is None


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


def test_exotic_runs_before_tanfuku(monkeypatch):
    """組合せは単複より先に見る。**空き時間に回すと永久に取れない。**

    締切 10 分前は単複の勝負どころスロット（T-10/8/6/4/2）が最も密な
    時間帯で、_pick がほぼ必ず何かを返す。空き待ちにすると _run_exotic に
    到達しない（実運用で 30 分待って 1 件も取れずに発覚した）。
    """
    f = _fetch_mod(monkeypatch)
    now = _at("14:55")
    races = [_race("20260826-oi-01", "15:00")]

    calls = []
    monkeypatch.setattr(f, "_races_today", lambda d: races)
    monkeypatch.setattr(f, "_run_exotic",
                        lambda *a: calls.append("exotic") or {"exotic": "umatan"})
    monkeypatch.setattr(f, "_pick",
                        lambda *a: calls.append("pick") or None)

    out = f.run(now)
    # 単複の候補があっても組合せが先に返る
    assert calls == ["exotic"]
    assert out == {"exotic": "umatan"}


def test_tanfuku_still_runs_when_no_exotic(monkeypatch):
    """組合せの仕事が無ければ単複に落ちる（既存の経路を壊さない）。"""
    f = _fetch_mod(monkeypatch)
    now = _at("13:00")
    races = [_race("20260826-oi-01", "15:00")]

    monkeypatch.setattr(f, "_races_today", lambda d: races)
    monkeypatch.setattr(f, "_run_exotic", lambda *a: None)
    monkeypatch.setattr(f, "_pick", lambda *a: None)
    monkeypatch.setattr(f, "_run_record", lambda *a: {"record": 1})

    assert f.run(now) == {"record": 1}


# 組合せの歪みの閾値（#56）
def test_exotic_edge_threshold_is_two_sigma():
    """**回収率を見る前に固定した値。** 緩めるなら検証をやり直す。

    2569 レース / 112,262 点の分布（平均 -0.560 / σ 0.928）から +2σ。
    単勝（#117）が +2σ を採ったのと同じ絞り込みの程度。
    """
    from ingest.exotic import (EXOTIC_EDGE_MEAN, EXOTIC_EDGE_SD,
                               EXOTIC_EDGE_THRESHOLD)
    assert (EXOTIC_EDGE_MEAN, EXOTIC_EDGE_SD) == (-0.560, 0.928)
    assert EXOTIC_EDGE_THRESHOLD == EXOTIC_EDGE_MEAN + 2.0 * EXOTIC_EDGE_SD
    assert round(EXOTIC_EDGE_THRESHOLD, 3) == 1.296


def test_exotic_edge_pick_at_threshold():
    """閾値ちょうどは拾う（>= で判定）。form.is_edge_pick と揃える。"""
    from ingest.exotic import EXOTIC_EDGE_THRESHOLD, is_exotic_edge_pick
    assert is_exotic_edge_pick(EXOTIC_EDGE_THRESHOLD)
    assert not is_exotic_edge_pick(EXOTIC_EDGE_THRESHOLD - 1e-9)


def test_exotic_edge_pick_ignores_missing():
    """乖離が無い組（片方に値が無い）は候補にしない。"""
    from ingest.exotic import is_exotic_edge_pick
    assert not is_exotic_edge_pick(None)


def test_exotic_edge_threshold_is_positive_side_only():
    """**上側だけを見る。** 馬券は買うことしかできない。

    分布は負に寄っている（平均 -0.560）が、閾値は正の側にある。
    負の大きい値（市場が高く見ている = 売り候補）を拾わないことを固定する。
    """
    from ingest.exotic import EXOTIC_EDGE_THRESHOLD, is_exotic_edge_pick
    assert EXOTIC_EDGE_THRESHOLD > 0
    assert not is_exotic_edge_pick(-3.0)      # 下側は候補にしない
    assert not is_exotic_edge_pick(-0.560)    # 平均も候補にしない
