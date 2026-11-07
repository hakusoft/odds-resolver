"""朝ジョブのやり直し（#175）のテスト。ネットワーク・AWS 非依存。"""
import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


class _Table:
    """pk ごとに項目を持つだけの DynamoDB もどき。"""

    def __init__(self):
        self.items = []

    def put_item(self, Item):
        self.items.append(Item)

    def query(self, KeyConditionExpression, Limit=None, **kw):
        pk = KeyConditionExpression.get_expression()["values"][1]
        got = [it for it in self.items if it["pk"] == pk]
        return {"Items": got[:Limit] if Limit else got}

    def pks(self, prefix):
        return [it for it in self.items if it["pk"].startswith(prefix)]


VENUES = [{"venue": "大井", "key": "202610220001"},
          {"venue": "門別", "key": "202610220002"}]
RACES = [{"race_no": n, "post_time": "15:00", "name": f"R{n}", "n_horses": 10,
          "surface": "ダ", "distance": 1200} for n in (1, 2)]


@pytest.fixture
def morning(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dummy")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    from ingest import morning as m
    importlib.reload(m)
    table = _Table()
    monkeypatch.setattr(m, "_TABLE", table)
    calls = []
    monkeypatch.setattr(m.source, "fetch", lambda path: calls.append(path) or path)
    monkeypatch.setattr(m, "parse_day_list", lambda html, date: VENUES)
    monkeypatch.setattr(m, "parse_race_list", lambda html, venue: RACES)
    m._calls, m._table = calls, table
    return m


def test_first_run_writes_races_and_marks_done(morning):
    out = morning.run("20261022")
    assert out["races"] == 4 and out["failed"] == 0
    assert len(morning._table.pks("DAY#20261022")) == 4
    assert len(morning._table.pks("MORNING#20261022")) == 1


def test_done_day_does_not_touch_source(morning):
    """**完了印があれば取得元に触れない。** 毎時の再試行で枠を使わない。"""
    morning.run("20261022")
    morning._calls.clear()
    assert morning.run("20261022")["note"] == "already done"
    assert morning._calls == []


def test_top_failure_raises_and_leaves_no_mark(morning, monkeypatch):
    """**10/22 の 503。** トップが落ちたら印を付けず、次の起動に任せる。"""
    def _503(path):
        raise OSError("HTTP Error 503")
    monkeypatch.setattr(morning.source, "fetch", _503)
    with pytest.raises(OSError):
        morning.run("20261022")
    assert morning._table.pks("MORNING#") == []


def test_venue_failure_retried_next_run_without_duplicates(morning, monkeypatch):
    """**欠けた会場だけ取り直す。** 揃った会場は 2 回目に取得しない。"""
    def _flaky(html, venue):
        if venue == "門別":
            raise ValueError("parse failed")
        return RACES
    monkeypatch.setattr(morning, "parse_race_list", _flaky)
    out = morning.run("20261022")
    assert out["failed"] == 1
    assert morning._table.pks("MORNING#") == []      # 揃っていないので印なし

    monkeypatch.setattr(morning, "parse_race_list", lambda h, v: RACES)
    morning._calls.clear()
    out = morning.run("20261022")
    assert out["skipped"] == 1 and out["failed"] == 0
    # トップ + 門別の 2 回だけ。大井は取り直さない
    assert len(morning._calls) == 2
    assert {it["venue"] for it in morning._table.pks("DAY#")} == {"大井", "門別"}
    assert len(morning._table.pks("DAY#")) == 4
    assert len(morning._table.pks("MORNING#")) == 1


def test_no_meeting_is_marked_done(morning, monkeypatch):
    """非開催日も印を付ける。付けないと毎時トップを叩き続ける。"""
    monkeypatch.setattr(morning, "parse_day_list", lambda html, date: [])
    assert morning.run("20261022")["note"] == "no meeting"
    assert len(morning._table.pks("MORNING#")) == 1
    assert morning._table.pks("DAY#") == []


def test_done_mark_is_outside_day_partition(morning):
    """**完了印を DAY# に置かない。** fetch は DAY#{date} を全件レースとして読む。"""
    morning.run("20261022")
    assert all("race_id" in it for it in morning._table.pks("DAY#"))
