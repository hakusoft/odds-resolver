"""取得した原本を S3 に残す（#172）のテスト。ネットワーク・AWS 非依存。"""
import gzip
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ingest import source  # noqa: E402


class _Res:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _S3:
    def __init__(self, fail=False):
        self.puts, self.fail = [], fail

    def put_object(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.puts.append(kw)


def _setup(monkeypatch, s3, bucket="data-bucket"):
    if bucket:
        monkeypatch.setenv("RAW_BUCKET", bucket)
    else:
        monkeypatch.delenv("RAW_BUCKET", raising=False)
    monkeypatch.setattr(source, "_s3", s3)
    monkeypatch.setattr(source, "_last_fetch", 0.0)
    monkeypatch.setattr(source.urllib.request, "urlopen",
                        lambda req, timeout: _Res("<html>オッズ</html>"))


def test_raw_key_is_jst_dated():
    # 2026-10-22 15:00:05 UTC = 2026-10-23 00:00:05 JST（日付は JST で切る）
    ts = 1792681205
    k = source.raw_key("/odds/tanfuku/RACEID/202610230101", ts)
    assert k == "html/20261023/odds_tanfuku_RACEID_202610230101/000005.html.gz"
    assert source.raw_key("/", ts).startswith("html/20261023/top/")


def test_fetch_saves_gzipped_body(monkeypatch):
    s3 = _S3()
    _setup(monkeypatch, s3)
    assert source.fetch("/race_card/list/RACEID/1") == "<html>オッズ</html>"
    (put,) = s3.puts
    assert put["Bucket"] == "data-bucket"
    assert put["Key"].startswith("html/")
    assert gzip.decompress(put["Body"]).decode() == "<html>オッズ</html>"


def test_save_failure_does_not_break_fetch(monkeypatch, capsys):
    """**保存の失敗で取得を止めない。** 本文はそのまま返る。"""
    _setup(monkeypatch, _S3(fail=True))
    assert source.fetch("/x") == "<html>オッズ</html>"
    assert "raw save failed" in capsys.readouterr().err


def test_no_bucket_no_save(monkeypatch):
    """RAW_BUCKET が無ければ S3 に触らない（テスト・手元実行）。"""
    s3 = _S3()
    _setup(monkeypatch, s3, bucket=None)
    source.fetch("/x")
    assert s3.puts == []


def test_client_has_short_timeout_and_no_retry(monkeypatch):
    """**S3 が詰まっても fetch（timeout 55 秒）を道連れにしない。**"""
    import boto3
    seen = {}
    monkeypatch.setattr(boto3, "client",
                        lambda name, config=None: seen.update(c=config) or _S3())
    monkeypatch.setenv("RAW_BUCKET", "b")
    monkeypatch.setattr(source, "_s3", None)
    source._save_raw("/x", "body")
    c = seen["c"]
    assert c.connect_timeout + c.read_timeout < 10
    assert c.retries["max_attempts"] == 1
