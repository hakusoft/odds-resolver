"""API の指標計算（ネットワーク非依存の純粋部分）。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


def test_metrics_shape(monkeypatch):
    # _latest_metrics は DynamoDB を叩くので、_query をスタブして計算部分だけ検証
    monkeypatch.setenv("TABLE_NAME", "dummy")
    import importlib
    from ingest import api
    importlib.reload(api)

    monkeypatch.setattr(api, "_query", lambda *a, **k: [
        {"odds": [2.0, 4.0, 8.0, 8.0]}  # 支持率 0.5/0.25/0.125/0.125
    ])
    top1, ent = api._latest_metrics("x")
    assert abs(top1 - 0.5) < 1e-6
    assert 0 < ent < 1  # 正規化エントロピー
