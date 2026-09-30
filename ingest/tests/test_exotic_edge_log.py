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


def test_umafuku_ignores_order_within_two_places():
    """馬複は順不同だが 1-2 着を占める必要がある。"""
    assert _exotic_hit("umafuku", "3-5", ORDER) is True
    assert _exotic_hit("umafuku", "5-3", ORDER) is True


def test_umafuku_miss_when_horse_is_third():
    """3 着では馬複は当たらない（ワイドとの差がここ）。"""
    assert _exotic_hit("umafuku", "5-8", ORDER) is False


def test_wide_hits_when_both_within_third():
    """**ワイドは 2 頭がともに 3 着以内なら当たり。** 着順は問わない。

    以前は点数（2）をそのまま見る着順にしていたため 1-2 着しか見ず、
    3 着に入った側を外れにしていた（#56 の判定後に発見）。
    """
    assert _exotic_hit("wide", "5-3", ORDER) is True    # 1-2 着
    assert _exotic_hit("wide", "5-8", ORDER) is True    # 1-3 着 ← 旧実装は False
    assert _exotic_hit("wide", "3-8", ORDER) is True    # 2-3 着 ← 旧実装は False
    assert _exotic_hit("wide", "8-5", ORDER) is True    # 順不同


def test_wide_miss_when_one_horse_is_fourth():
    """1 頭でも 4 着以下なら外れ。"""
    assert _exotic_hit("wide", "5-1", ORDER) is False   # 1 着と 4 着
    assert _exotic_hit("wide", "1-2", ORDER) is False   # 4 着と 5 着


def test_wide_needs_three_places_to_decide():
    """**2 着までしか無いワイドは None。** 当落が確定しない。

    点数（2）で足りるか判定していたため、旧実装は 2 着までで True/False を
    返していた。3 着に入るかどうかが未定なのに外れを確定させてしまう。
    """
    assert _exotic_hit("wide", "5-3", [5, 3]) is None
    assert _exotic_hit("umafuku", "5-3", [5, 3]) is True    # 馬複は 2 着で確定


def test_hit_is_none_on_unknown_kind():
    """知らない券種は判定しない（黙って順不同扱いにしない）。"""
    assert _exotic_hit("tansho", "5", ORDER) is None


def test_hit_is_none_when_combo_size_mismatches_kind():
    """**券種に合わない点数は None。** 判定できないものを外れにしない。

    「頭数」と「着数」を分けたとき、点数を検証しないと三連複に 2 頭を
    渡した場合にワイドと同じ枝（3 着以内に入っていれば当たり）へ落ちて
    しまう。旧実装では sorted 比較で False になっていた経路。
    """
    assert _exotic_hit("sanrenfuku", "5-3", ORDER) is None    # 3 頭券種に 2 頭
    assert _exotic_hit("wide", "5-3-8", ORDER) is None        # 2 頭券種に 3 頭
    assert _exotic_hit("wide", "5", ORDER) is None            # 2 頭券種に 1 頭
    assert _exotic_hit("umatan", "5-3-8", ORDER) is None


def test_hit_is_none_on_duplicate_horse():
    """同じ馬番を 2 回含む組は None。買えない組なので判定しない。"""
    assert _exotic_hit("wide", "5-5", ORDER) is None
    assert _exotic_hit("umafuku", "5-5", ORDER) is None


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
