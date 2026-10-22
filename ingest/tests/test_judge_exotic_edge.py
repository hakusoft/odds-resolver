"""組合せの歪みの判定ツール（#56）のテスト。ネットワーク非依存。

**基準を固定するのがこのテストの仕事。** MIN_N と「100% を跨いだら効果なし」
を書き換えたら落ちる。判定日に緩めるのを防ぐため。
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.tools.judge_exotic_edge import (  # noqa: E402
    MIN_HITS, MIN_N, MIN_N_BY_KIND, min_hits, min_n, tally, verdict)


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
    assert verdict(t).startswith("判定不可（n ")


def test_min_n_is_per_kind():
    """**券種ごとの n（#169）。** ワイドは 500、既定は 300。"""
    assert MIN_N_BY_KIND["wide"] == 500
    assert min_n("wide") == 500
    assert min_n("umatan") == MIN_N == 300     # 判定済み。据え置き
    assert min_n("sanrenfuku") == 300          # 未定の券種は既定


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


# --- 的中数の要件（#165）---------------------------------------------------

def test_min_hits_is_fixed_per_kind():
    """**券種ごとに先に決めた基準。** 判定日に緩めるのを防ぐ。"""
    assert MIN_HITS["wide"] == 10
    assert MIN_HITS["umatan"] == 0       # 判定済み。遡って変えない
    assert min_hits("wide") == 10
    assert min_hits("sanrenfuku") == 0   # 未定の券種は n だけで判定


def test_wide_blocked_when_hits_too_few():
    """**#56 の失敗をここで止める。** n は足りても的中が足りなければ判定しない。

    馬単は n=367 / 的中 1 件で「効果あり（107.36%）」が出たが、5 日後に
    n=806 / 的中 1 件のまま 48.88% へ落ちた。1 件の当落で結論が振れる
    状態で回収率を読んではいけない。
    """
    # n は足りている（ワイドは 500 必要）。的中 1 件で回収率 107% を作る
    t = tally(_rows(500, hits=1, odds=537.0))
    assert t["n"] >= min_n("wide")
    assert t["payback"] > 1.0
    assert verdict(t, "wide") == "判定不可（的中 1 件 < 10 件）"


def test_wide_judged_once_hits_reached():
    """n と的中の両方が基準に達すれば判定する。"""
    t = tally(_rows(500, hits=10, odds=40.0))
    assert verdict(t, "wide") == "効果なし"     # 400/500 = 80.0%
    t2 = tally(_rows(500, hits=10, odds=55.0))
    assert verdict(t2, "wide") == "効果あり"    # 550/500 = 110.0%


def test_wide_n_shortage_reported_before_hits():
    """n も的中も足りないときは n 不足を先に言う（直す順が決まる）。"""
    t = tally(_rows(100, hits=1, odds=10.0))
    assert verdict(t, "wide") == "判定不可（n 100 < 500）"


def test_wide_n_300_is_not_enough():
    """**ワイドは n=300 では足りない（#169）。** 的中が揃っていても。

    分布を引き直したらオッズ帯が上がり、n=300 の期待的中が 6.1 件に
    落ちた。500 で 10.2 件になる。
    """
    t = tally(_rows(300, hits=12, odds=40.0))
    assert t["hits"] >= min_hits("wide")       # 的中は足りている
    assert verdict(t, "wide") == "判定不可（n 300 < 500）"


def test_umatan_verdict_unchanged():
    """**馬単の判定は変えない。** 2026-09-30 に判定済み。

    遡って基準を変えると「後から都合よく動かした」ことになる。
    #56 の判定を今の基準で再計算できてしまわないようにする。
    """
    # #56 の判定時と同じ形（n=367 / 的中 1 件 / オッズ 394）
    t = tally(_rows(367, hits=1, odds=394.0))
    assert verdict(t, "umatan") == "効果あり"   # 当時と同じ結論
    assert verdict(t) == "効果あり"             # kind 無しでも同じ


def test_verdict_without_kind_uses_defaults():
    """kind を渡さなければ既定の MIN_N を使い、的中数を見ない（後方互換）。"""
    t = tally(_rows(300, hits=1, odds=400.0))
    assert verdict(t) == "効果あり"
    # ワイドとして見れば n が足りない（500 必要）
    assert verdict(t, "wide") == "判定不可（n 300 < 500）"


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

    # edge は馬単の閾値（+1.295）以上にする。#169 で閾値未満の行も
    # 落とすようにしたため
    payload = {"rows": [
        {"kind": "umatan", "hit": True, "odds": 10.0, "edge": 2.0},
        {"kind": "umatan", "hit": False, "odds": 5.0, "edge": 2.0},
        {"kind": "umatan", "hit": None, "odds": 7.0, "edge": 2.0},   # 着順不足
        {"kind": "sanrenfuku", "hit": True, "odds": 99.0, "edge": 2.0},  # 別券種
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


def test_load_rows_drops_below_current_threshold(monkeypatch):
    """**いまの閾値を下回る行は落とす（#169）。**

    閾値を引き直したとき、旧閾値で記録された行が残っている。混ぜると
    「先に決めた閾値で記録した」ことにならない。

    実データでは旧閾値 1.8555 の記録 568 点のうち、新閾値 1.9315 以上は
    459 点だった。
    """
    import json as _json
    from ingest.exotic import exotic_threshold
    from ingest.tools import judge_exotic_edge as J

    t = exotic_threshold("wide")
    payload = {"rows": [
        {"kind": "wide", "hit": True, "odds": 20.0, "edge": t},          # 境界
        {"kind": "wide", "hit": False, "odds": 30.0, "edge": t + 0.5},
        {"kind": "wide", "hit": True, "odds": 25.0, "edge": t - 0.001},  # 旧閾値
        {"kind": "wide", "hit": False, "odds": 40.0, "edge": None},      # edge 無し
    ]}

    class _S3:
        def list_objects_v2(self, **kw):
            return {"Contents": [{"Key": "exotic_edge/20261017.json"}],
                    "IsTruncated": False}

        def get_object(self, **kw):
            return {"Body": type("B", (), {
                "read": staticmethod(lambda: _json.dumps(payload).encode())})()}

    monkeypatch.setattr(J, "_get_s3", lambda: _S3())
    rows = J.load_rows("b", "wide")
    assert len(rows) == 2                    # 境界は含む、閾値未満と None は落とす
    assert all(float(r["edge"]) >= t for r in rows)


# --- 判定は 1 回だけ（#171）-------------------------------------------------

class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _JudgeS3:
    """judgments/ への条件付き書き込みを真似る。exotic_edge/ は固定の行。"""

    def __init__(self, rows, store=None):
        self.rows = rows
        self.store = dict(store or {})
        self.puts = 0

    def list_objects_v2(self, **kw):
        return {"Contents": [{"Key": "exotic_edge/20261020.json"}],
                "IsTruncated": False}

    def get_object(self, Bucket, Key):
        import json as _json
        if Key.startswith("judgments/"):
            if Key not in self.store:
                raise _ClientError("NoSuchKey")
            body = self.store[Key]
        else:
            body = _json.dumps({"rows": self.rows}).encode()
        return {"Body": type("B", (), {"read": staticmethod(lambda: body)})()}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, **kw):
        self.puts += 1
        if IfNoneMatch == "*" and Key in self.store:
            raise _ClientError("PreconditionFailed")
        self.store[Key] = Body


def _wide_rows(n, hits, odds=60.0):
    from ingest.exotic import exotic_threshold
    t = exotic_threshold("wide")
    return ([{"kind": "wide", "hit": True, "odds": odds, "edge": t}] * hits
            + [{"kind": "wide", "hit": False, "odds": odds, "edge": t}]
            * (n - hits))


def _must_not_read(*a):
    raise AssertionError("判定済みなのにログを読んだ")


def test_judge_writes_once_and_second_run_does_not_recompute(
        monkeypatch, capsys):
    """**2 回目は計算しない。** 1 回目の記録をそのまま返す。"""
    import json as _json
    from ingest.tools import judge_exotic_edge as J

    s3 = _JudgeS3(_wide_rows(500, 10))
    monkeypatch.setattr(J, "_get_s3", lambda: s3)
    assert J.main(["--kind", "wide", "--bucket", "b"]) == 0
    first = _json.loads(capsys.readouterr().out)
    assert first["n"] == 500 and first["verdict"] == "効果あり"
    assert J.judgment_key("wide") in s3.store

    # 行が増えて率が変わっても、2 回目は新しい数字を出さない
    s3.rows = _wide_rows(800, 10)
    monkeypatch.setattr(J, "load_rows", _must_not_read)
    assert J.main(["--kind", "wide", "--bucket", "b"]) == 0
    second = _json.loads(capsys.readouterr().out)
    assert second == first
    assert s3.puts == 1


def test_judge_loses_race_shows_winner(monkeypatch, capsys):
    """**読んだ後に別の実行が書いた場合も、先客の判定を返す。**"""
    import json as _json
    from ingest.tools import judge_exotic_edge as J

    s3 = _JudgeS3(_wide_rows(500, 10))
    winner = {"kind": "wide", "n": 501, "verdict": "効果なし"}
    real_load = J.load_judgment
    calls = []

    def _load(bucket, kind):
        calls.append(kind)
        if len(calls) == 1:
            # 最初の確認時点ではまだ無い。直後に先客が書く
            s3.store[J.judgment_key(kind)] = _json.dumps(winner).encode()
            return None
        return real_load(bucket, kind)

    monkeypatch.setattr(J, "_get_s3", lambda: s3)
    monkeypatch.setattr(J, "load_judgment", _load)
    assert J.main(["--kind", "wide", "--bucket", "b"]) == 0
    assert _json.loads(capsys.readouterr().out) == winner


def test_judge_does_not_print_when_save_fails(monkeypatch, capsys):
    """**書けなければ表示しない。** 記録の無い判定を見ると、もう 1 回打てる。"""
    from ingest.tools import judge_exotic_edge as J

    class _Broken(_JudgeS3):
        def put_object(self, **kw):
            raise _ClientError("AccessDenied")

    monkeypatch.setattr(J, "_get_s3", lambda: _Broken(_wide_rows(500, 10)))
    with pytest.raises(_ClientError):
        J.main(["--kind", "wide", "--bucket", "b"])
    assert "payback" not in capsys.readouterr().out


def test_umatan_is_already_judged(monkeypatch, capsys):
    """**馬単は 2026-09-30 に判定済み。** judgments/ が無くても再判定させない。"""
    import json as _json
    from ingest.tools import judge_exotic_edge as J

    s3 = _JudgeS3([])
    monkeypatch.setattr(J, "_get_s3", lambda: s3)
    assert J.main(["--kind", "umatan", "--bucket", "b"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["judged_at"] == "2026-09-30"
    assert out["payback"] == 1.0736
    assert s3.puts == 0


def test_check_reports_judged_without_rate(monkeypatch, capsys):
    import json as _json
    from ingest.tools import judge_exotic_edge as J

    monkeypatch.setattr(J, "_get_s3", lambda: _JudgeS3(_wide_rows(10, 1)))
    assert J.main(["--kind", "wide", "--bucket", "b", "--check"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["judged"] is False
    assert "payback" not in out
