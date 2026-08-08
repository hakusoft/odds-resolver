"""汚染された jockey フィールドを欠測（null）に落とす（#118）。

#112 のパースバグで、S3 に焼かれた馬柱データの `records[].jockey` に騎手名では
なく斤量の値が入っている。パース自体は #113 で直ったが、既に焼かれた分は汚染
されたまま残る。

`"56.0"` は型としては正常な文字列なので、後段の分析は何の警告も出さずに騎手名
として扱う。騎手別に集計すれば「56.0 という騎手」が生まれる。欠測なら弾けるが
汚染は弾けない。存在しない値は「無い」と表現するのが正しい。

**騎手名は復元しない。** 復元には過去ページの再取得が要り、それは #111 の領分。
ここは「間違った値を残さない」ところまで。

archive.recalc() が races/*.json を読むだけで書き換えないのに対し、これは
**書き換える唯一の経路**。夜間バッチに混ぜず手元から回す。

使い方:

    # 何が変わるか見るだけ（既定。書き込まない）
    python -m ingest.tools.fix_jockey

    # 実際に書き込む
    python -m ingest.tools.fix_jockey --apply

    # 特定日だけ
    python -m ingest.tools.fix_jockey --date 20260731 --apply

環境変数 DATA_BUCKET / FRONTEND_BUCKET を見る（archive と同じ）。
"""
import argparse
import json
import os
import sys

import boto3

from ..archive import _CC_RACE
from ..parse import is_contaminated_jockey

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _iter_keys(bucket: str, prefix: str):
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        res = _get_s3().list_objects_v2(**kw)
        for obj in res.get("Contents", []):
            yield obj["Key"]
        if not res.get("IsTruncated"):
            break
        token = res.get("NextContinuationToken")


def scrub(race: dict) -> int:
    """1 レース分の records から汚染した jockey を落とす。落とした頭数を返す。

    キーごと消さず None を入れるのは、「取得したが値が無い」と「そもそも項目が
    無い」を後段が区別できるようにするため。
    """
    n = 0
    for rec in race.get("records") or []:
        if is_contaminated_jockey(rec.get("jockey")):
            rec["jockey"] = None
            n += 1
    return n


def run(date: str | None = None, apply: bool = False) -> dict:
    data_bucket = os.environ["DATA_BUCKET"]
    frontend_bucket = os.environ["FRONTEND_BUCKET"]
    prefix = f"races/{date}-" if date else "races/"

    s3 = _get_s3()
    scanned = 0
    touched = 0
    horses = 0
    for key in _iter_keys(data_bucket, prefix):
        scanned += 1
        body = s3.get_object(Bucket=data_bucket, Key=key)["Body"].read()
        race = json.loads(body)
        n = scrub(race)
        if not n:
            continue
        touched += 1
        horses += n
        if not apply:
            continue
        data = json.dumps(race, ensure_ascii=False).encode("utf-8")
        # archive._put と同じ 2 バケット構成・同じ Cache-Control を使う。
        # ここだけ値が違うと、書き戻したファイルのキャッシュ挙動が archive の
        # 焼いたものとズレる。
        for bucket, pfx in ((data_bucket, ""), (frontend_bucket, "data/")):
            s3.put_object(
                Bucket=bucket, Key=pfx + key, Body=data,
                ContentType="application/json; charset=utf-8",
                CacheControl=_CC_RACE,
            )
    return {"scanned": scanned, "races_fixed": touched, "horses_fixed": horses,
            "applied": apply}


def verify(date: str | None = None) -> dict:
    """汚染が残っていないか、両バケットを実際に読んで数える。"""
    prefix = f"races/{date}-" if date else "races/"
    s3 = _get_s3()
    out = {}
    for label, bucket, pfx in (
        ("data", os.environ["DATA_BUCKET"], ""),
        ("frontend", os.environ["FRONTEND_BUCKET"], "data/"),
    ):
        bad = 0
        files = 0
        for key in _iter_keys(bucket, pfx + prefix):
            files += 1
            race = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            bad += sum(1 for r in (race.get("records") or [])
                       if is_contaminated_jockey(r.get("jockey")))
        out[label] = {"files": files, "contaminated": bad}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="YYYYMMDD。省略すると全期間")
    ap.add_argument("--apply", action="store_true",
                    help="実際に書き込む（既定は dry-run）")
    ap.add_argument("--verify", action="store_true",
                    help="書き込まず、汚染が残っていないかだけ数える")
    a = ap.parse_args(argv)

    if a.verify:
        print(json.dumps(verify(a.date), ensure_ascii=False, indent=2))
        return 0

    res = run(a.date, a.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not a.apply:
        print("\ndry-run。書き込むには --apply を付ける。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
