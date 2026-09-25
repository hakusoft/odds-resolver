"""組合せの歪みの前向きログ（#56）のテスト。ネットワーク非依存。

**的中判定が中心。** 券種ごとに当たりの定義が違うので、ここを間違えると
回収率が丸ごと狂う。単勝側（#117）は「1 着かどうか」だけだったが、
組合せは順序の有無で判定が変わる。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.archive import _exotic_hit  # noqa: E402

# 着順（1 着から順に馬番）
ORDER = [5, 3, 8, 1, 2]


def test_umatan_hit_needs_exact_order():
    """馬単は 1着→2着 が順番どおり。"""
    assert _exotic_hit("umatan", "5-3", ORDER) is True
    assert _exotic_hit("umatan", "3-5", ORDER) is False   # 逆は外れ


def test_sanrentan_hit_needs_exact_order():
    """三連単は 1-2-3 着が順番どおり。"""
    assert _exotic_hit("sanrentan", "5-3-8", ORDER) is True
    assert _exotic_hit("sanrentan", "5-8-3", ORDER) is False


def test_sanrenfuku_hit_ignores_order():
    """三連複は順不同。3 頭が 1-3 着を占めていれば当たり。"""
    assert _exotic_hit("sanrenfuku", "5-3-8", ORDER) is True
    assert _exotic_hit("sanrenfuku", "8-5-3", ORDER) is True
    assert _exotic_hit("sanrenfuku", "3-8-5", ORDER) is True


def test_sanrenfuku_miss_when_wrong_horse():
    """1 頭でも違えば外れ（順不同でも 3 着以内である必要がある）。"""
    assert _exotic_hit("sanrenfuku", "5-3-1", ORDER) is False


def test_umafuku_and_wide_ignore_order():
    """馬複・ワイドも順不同扱い（将来取得した時のため）。

    **ワイドは本来 3 着以内 2 頭なので、この判定では厳しすぎる。**
    取得する時に定義を見直すこと。現状 EXOTIC_KINDS に入っていない。
    """
    assert _exotic_hit("umafuku", "3-5", ORDER) is True
    assert _exotic_hit("umafuku", "5-3", ORDER) is True


def test_hit_is_none_when_result_too_short():
    """**着順が足りなければ None。外れではない。**

    取消・中止で着順が揃わないレースを「外れ」にすると分母だけ増えて
    回収率が不当に下がる（_append_edge_log が pos 無しを外れにしないのと
    同じ理由）。
    """
    assert _exotic_hit("sanrenfuku", "5-3-8", [5, 3]) is None
    assert _exotic_hit("umatan", "5-3", []) is None


def test_hit_is_none_on_malformed_input():
    assert _exotic_hit("umatan", None, ORDER) is None
    assert _exotic_hit(None, "5-3", ORDER) is None
    assert _exotic_hit("umatan", "a-b", ORDER) is None


def test_hit_only_looks_at_needed_places():
    """馬単は 3 着以降を見ない。余分な着順があっても判定は変わらない。"""
    assert _exotic_hit("umatan", "5-3", [5, 3, 99, 98]) is True


# fetch 側の記録（#56）
def _fetch_mod(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from unittest import mock
    with mock.patch("boto3.resource"):
        from ingest import fetch
        importlib.reload(fetch)
    return fetch


def _matrix(nums):
    import itertools
    return {(a, b): float((a * 7 + b * 3) % 90 + 2)
            for a, b in itertools.permutations(nums, 2)}


def test_record_writes_only_picks_over_threshold(monkeypatch):
    """閾値を超えた組だけ書く。全部書くと n が水増しされる。"""
    f = _fetch_mod(monkeypatch)
    nums = [1, 2, 3, 4, 5, 6]
    m = _matrix(nums)
    m[(1, 2)] = 900.0          # ここだけ市場が極端に安く見ている
    puts = []
    monkeypatch.setattr(f, "_latest_snapshot",
                        lambda rid: ([2.0, 4.0, 8.0, 10.0, 20.0, 40.0], nums))
    monkeypatch.setattr(f, "_TABLE", type("T", (), {
        "put_item": staticmethod(lambda **kw: puts.append(kw["Item"]))})())

    n = f._record_exotic_edges({"race_id": "R1"}, "umatan", m, 1700000000.0)
    assert n == 1
    assert puts[0]["sk"] == "XEDGE#umatan#1-2"
    assert puts[0]["combo"] == "1-2"


def test_record_keeps_raw_odds_for_payback(monkeypatch):
    """**素のオッズを残す。** p_market は正規化済みで元値に戻せない。

    回収率の計算にこれが要る（#117 Phase 3 と同じ理由）。
    """
    f = _fetch_mod(monkeypatch)
    nums = [1, 2, 3, 4, 5, 6]
    m = _matrix(nums)
    m[(1, 2)] = 900.0
    puts = []
    monkeypatch.setattr(f, "_latest_snapshot",
                        lambda rid: ([2.0, 4.0, 8.0, 10.0, 20.0, 40.0], nums))
    monkeypatch.setattr(f, "_TABLE", type("T", (), {
        "put_item": staticmethod(lambda **kw: puts.append(kw["Item"]))})())

    f._record_exotic_edges({"race_id": "R1"}, "umatan", m, 1700000000.0)
    assert float(puts[0]["odds"]) == 900.0


def test_record_has_no_result_fields(monkeypatch):
    """**結果を入れない。** 答え合わせは archive が後日行う。

    予測時点の記録に結果が混ざると「先に確定していた」担保が崩れる。
    """
    f = _fetch_mod(monkeypatch)
    nums = [1, 2, 3, 4, 5, 6]
    m = _matrix(nums)
    m[(1, 2)] = 900.0
    puts = []
    monkeypatch.setattr(f, "_latest_snapshot",
                        lambda rid: ([2.0, 4.0, 8.0, 10.0, 20.0, 40.0], nums))
    monkeypatch.setattr(f, "_TABLE", type("T", (), {
        "put_item": staticmethod(lambda **kw: puts.append(kw["Item"]))})())

    f._record_exotic_edges({"race_id": "R1"}, "umatan", m, 1700000000.0)
    for bad in ("hit", "pos", "won", "result"):
        assert bad not in puts[0]


def test_record_skips_without_snapshot(monkeypatch):
    """単勝オッズが無ければ理論を作れない。何も書かない。"""
    f = _fetch_mod(monkeypatch)
    puts = []
    monkeypatch.setattr(f, "_latest_snapshot", lambda rid: None)
    monkeypatch.setattr(f, "_TABLE", type("T", (), {
        "put_item": staticmethod(lambda **kw: puts.append(kw["Item"]))})())

    assert f._record_exotic_edges({"race_id": "R1"}, "umatan",
                                  _matrix([1, 2, 3]), 1.0) == 0
    assert puts == []


def test_record_skips_without_matrix(monkeypatch):
    f = _fetch_mod(monkeypatch)
    monkeypatch.setattr(f, "_latest_snapshot", lambda rid: ([2.0], [1]))
    assert f._record_exotic_edges({"race_id": "R1"}, "umatan", None, 1.0) == 0
