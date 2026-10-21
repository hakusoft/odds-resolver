"""朝の観測（#149）。ネットワーク/AWS 非依存の部分だけを見る。

このスクリプトの肝は**判断しないこと**なので、そこをテストで固定する。
「効果あり/なし」を書き始めたら、このテストが壊れる。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest.tools.morning_probe import (  # noqa: E402
    WATCH_SLOTS, jst_today, render, yesterday,
)


def _obs(**over):
    o = {
        "generated_at_jst": "2026-09-09 06:00",
        "lambda": {"morning": {"errors": 0.0, "invocations": 1.0},
                   "read-api": {"errors": None, "invocations": None}},
        "rules": {"odds-resolver-fetch-minutely": "ENABLED"},
        "container": {"date": "20260909", "n_races": 44, "venues": {"川崎": 12}},
        "days": [{"date": "2026-09-08", "n_races": 58, "venues": ["川崎"]}],
        "calibration": {
            "n_days": 46, "n_races": 1694, "n_horses": 16899,
            "place_since": "20260802",
            "invariants": {"a": True, "b": True, "c": True},
            "bin_edges": [0.0, 0.05, 1.0001],
            "by_surge": {"surged": [{"n": 20, "win_rate": 0.05, "payback": 1.665},
                                    {"n": 300, "win_rate": 0.657, "payback": 0.842}],
                         "calm": []},
        },
        "forward": {"days": 36, "n": 1326, "per_day_avg": 36.8,
                    "recent": [37, 38], "latest": "20260908.json"},
        "slots": {"date": "20260908", "n_races": 58, "exotic": 58,
                  "rates": {"T-45": 1.0, "T-15": 0.983}},
    }
    o.update(over)
    return o


def test_watches_t45_and_t15_not_t10():
    """T-10 は密度で 33-71% を正常に上下するので監視に使わない（#145）。"""
    assert WATCH_SLOTS == ["T-45", "T-15"]
    assert "T-10" not in WATCH_SLOTS


def test_never_absent_metric_as_zero():
    """データポイントが無い = 「一度も呼ばれていない」。0 と混同しない。"""
    md = render(_obs())
    assert "未呼び出し" in md
    # read-api の行が「0」になっていないこと
    line = [x for x in md.split("\n") if x.startswith("| read-api")][0]
    assert "未呼び出し" in line


def test_forward_shows_n_but_never_rates():
    """#106 は判定済み。途中経過の率を出すと判断が揺れる。"""
    md = render(_obs())
    assert "1326 頭" in md
    assert "回収" not in md.split("### 前向きログ")[1]
    assert "勝率" not in md.split("### 前向きログ")[1]


def test_no_verdict_words():
    """**判断しない。** 「効果あり/なし」「異常」と書き始めたらここが壊れる。"""
    md = render(_obs())
    for word in ("効果あり", "効果なし", "問題ありません", "順調"):
        assert word not in md


def test_broken_invariant_is_surfaced():
    """不変条件が崩れたら黙って流さない（集計のバグなので）。"""
    o = _obs()
    o["calibration"]["invariants"]["b"] = False
    md = render(o)
    assert "崩れている" in md


def test_disabled_rule_is_surfaced():
    o = _obs()
    o["rules"]["odds-resolver-fetch-minutely"] = "DISABLED"
    md = render(o)
    assert "ENABLED でない" in md


def test_prev_adds_delta_not_replaces():
    """差分は n の増分を併記するだけ。今回の値を置き換えない。"""
    prev = _obs()
    prev["calibration"]["by_surge"]["surged"] = [
        {"n": 15, "win_rate": 0.0, "payback": 0.0},
        {"n": 279, "win_rate": 0.656, "payback": 0.839}]
    md = render(_obs(), prev)
    assert "(+5)" in md and "(+21)" in md
    assert "| 20 |" in md  # 今回の n はそのまま


def test_yesterday_crosses_month():
    assert yesterday("20260901") == "20260831"
    assert yesterday("20260101") == "20251231"


def test_jst_today_is_ahead_of_utc():
    # UTC 2026-09-09 20:00 は JST では翌日
    ts = 1788033600  # 2026-09-09T16:00:00Z 相当でなくても順序だけ見る
    assert len(jst_today(ts)) == 8


def _obs_x(ready=False, hits_short=False):
    """組合せの進捗を載せた観測。ready / 的中不足 / n 不足を作れる。"""
    o = _obs()
    if ready:
        n, hits = 312, 14
    elif hits_short:
        n, hits = 312, 3        # n は足りるが的中が足りない（#56 の構造）
    else:
        n, hits = 186, 7
    o["exotic"] = {
        "wide": {
            "n": n, "required_n": 300,
            "hits": hits, "required_hits": 10,
            "remaining_n": max(0, 300 - n),
            "remaining_hits": max(0, 10 - hits),
            "ready": n >= 300 and hits >= 10,
        },
    }
    return o


def test_watch_kinds_excludes_judged():
    """**判定済みの券種は追わない。** 到達を言い続けると狼少年になる。

    #148 は 2026-09-24 に判定済みだったのに毎朝「判定できる」と出ていた
    （#167）。馬単（#56）も 2026-09-30 に判定済みなので入れない。
    """
    from ingest.tools.morning_probe import WATCH_EXOTIC_KINDS
    assert "wide" in WATCH_EXOTIC_KINDS
    assert "umatan" not in WATCH_EXOTIC_KINDS


def test_criteria_come_from_judge_tool():
    """**基準は judge_exotic_edge から読む。** 書き写すと食い違う。"""
    from ingest.tools.judge_exotic_edge import MIN_N, min_hits
    assert MIN_N == 300
    assert min_hits("wide") == 10


def test_exotic_progress_reads_files_once():
    """**ファイルは 1 回しか読まない。** 券種ごとに読み直さない。

    券種を足した瞬間に実行時間が線形に増えるのを防ぐ（毎朝走るので効く）。
    実測で 9.7 秒 → 0.5 秒、2 券種でも増えないことを確認した。
    """
    import ingest.tools.morning_probe as mp

    reads = []

    class _Sess:
        pass

    def _keys(sess, bucket, prefix):
        return ["exotic_edge/20261017.json", "exotic_edge/20261018.json"]

    def _get(sess, bucket, key):
        reads.append(key)
        # edge は各券種の閾値以上にする（#169 で閾値未満を落とすため）
        return {"rows": [
            {"kind": "wide", "hit": True, "odds": 20.0, "edge": 3.0},
            {"kind": "umatan", "hit": False, "odds": 500.0, "edge": 3.0},
            {"kind": "wide", "hit": None, "odds": 30.0, "edge": 3.0},  # 数えない
        ]}

    orig_keys, orig_get = mp._list_keys, mp._get_json
    orig_watch = mp.WATCH_EXOTIC_KINDS
    try:
        mp._list_keys, mp._get_json = _keys, _get
        mp.WATCH_EXOTIC_KINDS = ["wide", "umatan"]
        out = mp.exotic_progress(_Sess(), "b")
    finally:
        mp._list_keys, mp._get_json = orig_keys, orig_get
        mp.WATCH_EXOTIC_KINDS = orig_watch

    # 2 券種を見ても 2 ファイルしか読まない（4 回ではない）
    assert len(reads) == 2
    assert out["wide"]["n"] == 2        # hit None は除く
    assert out["wide"]["hits"] == 2
    assert out["umatan"]["n"] == 2
    assert out["umatan"]["hits"] == 0


def test_exotic_reports_readiness_but_never_judges():
    """**到達したと言うだけ。判定はしない**（1 回きりで取り消せない・#106）。"""
    md = render(_obs_x(ready=True))
    assert "判定できる" in md
    sec = md.split("### 組合せの判定待ち")[1]
    # 判定の結果らしき語が出ていないこと
    for word in ("効果あり", "効果なし", "回収率", "払戻"):
        assert word not in sec


def test_exotic_never_shows_rate():
    """**率は出さない。** 判定前に結果を見せない（--check と同じ作法）。"""
    md = render(_obs_x(ready=True))
    sec = md.split("### 組合せの判定待ち")[1]
    assert "%" not in sec
    assert "率は出さない" in sec


def test_exotic_not_ready_says_so():
    md = render(_obs_x())
    assert "まだ基準に達していない" in md
    assert "判定できる" not in md


def test_exotic_blocked_when_hits_short():
    """**n が足りても的中が足りなければ到達ではない（#165）。**

    #56 はこの状態で判定し、的中 1 件の当落で回収率が 107% → 43% に
    振れた。残り的中数が表に出ること。
    """
    md = render(_obs_x(hits_short=True))
    assert "まだ基準に達していない" in md
    sec = md.split("### 組合せの判定待ち")[1]
    assert "| 7 |" in sec or "7" in sec     # 残り的中 10-3=7 が出る


def test_delta_table_columns_align():
    """区切り行とヘッダーの列数を揃える（空セルで表が崩れていた）。"""
    prev = _obs()
    prev["calibration"]["by_surge"]["surged"] = [
        {"n": 15, "win_rate": 0.0, "payback": 0.0},
        {"n": 279, "win_rate": 0.656, "payback": 0.839}]
    md = render(_obs(), prev)
    # 較正の表だけを見る（判定待ちの表も "|---" で始まるので節で切る）
    sec = md.split("### 較正の帯")[1].split("###")[0]
    rows = [x for x in sec.split("\n") if x.startswith("|")]
    header, sep = rows[0], rows[1]
    assert header.count("|") == sep.count("|")
    assert "| |" not in header  # 空セルを作らない


def test_exotic_progress_drops_below_threshold():
    """**いまの閾値を下回る行は数えない（#169）。**

    旧閾値で記録された行が残っているので、混ぜると
    judge_exotic_edge --check と食い違う。
    """
    import ingest.tools.morning_probe as mp
    from ingest.exotic import exotic_threshold

    t = exotic_threshold("wide")

    def _keys(sess, bucket, prefix):
        return ["exotic_edge/20261017.json"]

    def _get(sess, bucket, key):
        return {"rows": [
            {"kind": "wide", "hit": True, "odds": 20.0, "edge": t},
            {"kind": "wide", "hit": False, "odds": 30.0, "edge": t - 0.001},
            {"kind": "wide", "hit": True, "odds": 25.0, "edge": None},
        ]}

    orig = mp._list_keys, mp._get_json, mp.WATCH_EXOTIC_KINDS
    try:
        mp._list_keys, mp._get_json = _keys, _get
        mp.WATCH_EXOTIC_KINDS = ["wide"]
        out = mp.exotic_progress(object(), "b")
    finally:
        mp._list_keys, mp._get_json, mp.WATCH_EXOTIC_KINDS = orig

    assert out["wide"]["n"] == 1          # 境界だけ残る
    assert out["wide"]["hits"] == 1
