"""組合せの歪みの判定ツール（#56）のテスト。ネットワーク非依存。

**基準を固定するのがこのテストの仕事。** MIN_N と「100% を跨いだら効果なし」
を書き換えたら落ちる。判定日に緩めるのを防ぐため。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.tools.judge_exotic_edge import (  # noqa: E402
    MIN_N, tally, verdict)


def _rows(n, hits=0, odds=10.0):
    out = [{"kind": "umatan", "hit": True, "odds": odds} for _ in range(hits)]
    out += [{"kind": "umatan", "hit": False, "odds": odds}
            for _ in range(n - hits)]
    return out


def test_min_n_is_300():
    """#56 で先に決めた基準。#106 / #117 と揃えてある。"""
    assert MIN_N == 300


def test_verdict_blocks_below_min_n():
    """n が足りなければ判定しない。"""
    # 回収率が 100% を大きく超えていても、n が足りなければ判定しない
    t = tally(_rows(MIN_N - 1, hits=MIN_N - 1, odds=10.0))
    assert t["payback"] > 1.0
    assert verdict(t) == "判定不可（n 不足）"


def test_verdict_no_effect_when_payback_under_100():
    """回収率 100% 未満は「効果なし」。"""
    # 300 点中 20 的中 × オッズ 10.0 = 200 / 300 = 66.7%
    assert verdict(tally(_rows(300, hits=20, odds=10.0))) == "効果なし"


def test_verdict_no_effect_at_exactly_100():
    """**ちょうど 100% は「効果なし」に倒す。** 有意でないものを有りとしない。"""
    # 300 点中 30 的中 × オッズ 10.0 = 300 / 300 = 100.0%
    t = tally(_rows(300, hits=30, odds=10.0))
    assert round(t["payback"], 6) == 1.0
    assert verdict(t) == "効果なし"


def test_verdict_effect_when_payback_over_100():
    # 300 点中 31 的中 × オッズ 10.0 = 310 / 300 = 103.3%
    assert verdict(tally(_rows(300, hits=31, odds=10.0))) == "効果あり"


def test_tally_payback_uses_raw_odds():
    """回収率は素のオッズから出す。p_market からは復元できない。"""
    t = tally([{"hit": True, "odds": 50.0}, {"hit": False, "odds": 3.0}])
    assert t["payback"] == 25.0      # 50 / 2
    assert t["hit_rate"] == 0.5


def test_tally_empty_is_zero():
    t = tally([])
    assert t == {"n": 0, "hits": 0, "hit_rate": 0.0, "payback": 0.0}


def test_load_rows_drops_unresolved_and_other_kinds(monkeypatch):
    """**hit が None の行は落とす。** 着順が足りなかっただけで外れではない。

    数えると分母だけ増えて回収率が不当に下がる（効果なし側に倒れる）。
    券種違いも落とす（#56 は券種ごとに判定する）。
    """
    import json as _json
    from ingest.tools import judge_exotic_edge as J

    payload = {"rows": [
        {"kind": "umatan", "hit": True, "odds": 10.0},
        {"kind": "umatan", "hit": False, "odds": 5.0},
        {"kind": "umatan", "hit": None, "odds": 7.0},      # 着順不足
        {"kind": "sanrenfuku", "hit": True, "odds": 99.0},  # 別券種
    ]}

    class _S3:
        def list_objects_v2(self, **kw):
            return {"Contents": [{"Key": "exotic_edge/20260927.json"}],
                    "IsTruncated": False}

        def get_object(self, **kw):
            return {"Body": type("B", (), {
                "read": staticmethod(
                    lambda: _json.dumps(payload).encode())})()}

    monkeypatch.setattr(J, "_get_s3", lambda: _S3())
    rows = J.load_rows("bucket", "umatan")
    assert len(rows) == 2                       # None と別券種は入らない
    assert all(r["kind"] == "umatan" for r in rows)
    assert all(r["hit"] is not None for r in rows)
