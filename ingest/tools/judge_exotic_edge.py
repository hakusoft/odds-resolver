"""組合せの歪み（#56）の判定を実行する。

券種ごとの閾値（`exotic.exotic_threshold`）を超えた組を買った場合、控除率の
壁を超えられるかを判定する。#106 / #117 と同じ枠組みで、**予測が結果より先に
確定していた**記録（`exotic_edge/*.json`）だけを使う。

**判定日に慌てて書かない。** 数字を見てからスクリプトを書くと、書き方自体が
結果に引きずられる。先に置くことで「後から都合よく変えていない」ことが git の
履歴で示せる（#123 / #148 / judge_edge と同じやり方）。

**このファイルを置いた時点で n=0。** 前向きログは 2026-09-27 に稼働を始めた
ばかりで、判定対象の行は 1 行も存在しない。

## 先に決めた基準（変えない）

kaz が 2026-09-27 に決定。**この時点で exotic_edge ログは 1 行も無い。**

- **n が基準に達するまで結論を出さない**（券種ごと・`MIN_N_BY_KIND`）
  - 馬単は 300（#106 / #117 と揃えた。2026-09-30 判定済み）
  - **ワイドは 500**（#169・2026-10-21）。分布を引き直したらオッズ帯が
    上がり、n=300 では期待的中が 6.1 件に落ちたため。500 で 10.2 件
- **的中数も要件にする（券種ごと・2026-10-14 追加・#165）**
  - **n=300 は行数の基準であって検出力の基準ではなかった。** 馬単は n=367 /
    的中 1 件で「効果あり（107.36%）」が出たが、5 日後に n=806 / 的中 1 件の
    まま回収率 48.88% へ落ちた。439 行積んで的中は増えず分母だけが増えた
  - ワイドは **的中>=10 件** も満たすまで判定しない（`MIN_HITS`）
  - **馬単は 0 のまま据え置く。** 2026-09-30 に判定済みなので遡って変えない
  - #106 / #148 / #56 と基準が変わるので横並びの比較はできない。
    **比較可能性より結論の信頼性を取った**
- **券種ごとに判定する**（`--kind` で明示させる）
  - 馬単と三連複は点数もオッズ帯も違う。混ぜると何を測ったか分からなくなる
  - **「馬単でダメだったから三連複で」は禁止。** 券種ごとに 1 回だけ
- **回収率が 100% を跨いだら「効果なし」に倒す**（有意でないものを有りとしない）
- **判定は 1 回だけ。** 「全体でダメだったからオッズ帯別で見る」は禁止
  - **コードで強制する（#171）。** 判定は `judgments/exotic_edge/{kind}.json`
    に条件付きで書き、既にあれば計算せず保存済みの結果を返す
- **検証期間中は閾値も理論式も動かさない**
  - 動かせば分布の前提が変わり、検証はやり直し
  - **ワイドは一度引き直した（#169）。** 分布測定ツールが記録と違う単勝
    スナップショットを見ていたため、測定と記録で入力が食い違っていた。
    「基準を先に決める」だけでは足りず、**記録と同じ計算で測る**必要が
    あったという教訓

## n の数え方

`exotic_edge/*.json` の全行のうち、**`hit` が None でなく、かつ `edge` が
いまの閾値以上のもの**。

閾値を引き直したとき旧閾値で記録された行が残るので、混ぜない（#169）。
記録そのものは壊れていない（記録済みの edge は修正後のツールの再計算と
558 点すべて一致した）。

`hit` が None なのは着順が足りなかったレース（取消・中止）で、「まだ結果が
出ていない」であって外れではない。数えると分母だけ増えて回収率が下がる
= 効果なし側に不当に倒れる（`judge_edge.load_rows` と同じ扱い）。

## 回収率の計算

的中した組の `odds` を足して n で割る。組合せオッズはそのまま払戻倍率なので、
単勝と同じ計算でよい。

使い方:

    export DATA_BUCKET=$(aws lambda get-function-configuration \\
      --function-name odds-resolver-archive \\
      --query 'Environment.Variables.DATA_BUCKET' --output text)

    python -m ingest.tools.judge_exotic_edge --kind umatan --check   # n だけ
    python -m ingest.tools.judge_exotic_edge --kind umatan           # 判定（1 回）
"""
import argparse
import datetime
import json
import os
import sys

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ingest.exotic import exotic_threshold  # noqa: E402

# #56 で先に決めた基準（既定値）。ここを緩めるなら検証をやり直す。
MIN_N = 300

# **券種ごとの最低 n（#169）。** 既定は MIN_N。
#
# ワイドは 500 にした（kaz 判断・2026-10-21）。#169 で分布を引き直したら
# オッズ帯が上がり、n=300 では的中数が足りなくなったため:
#
#   閾値 +1.5σ = 1.932  オッズ中央値 36.9  2.35 点/レース
#     n=300  期待的中  6.1 件  ← 「的中>=10 件」に届かない
#     n=500  期待的中 10.2 件  ← これを採る
#
# **倍率を下げる案も的中基準を下げる案も採らなかった。** 絞り込み
# （2.35 点/レース）を保ち、検出力は n で稼ぐ方が筋。#56 の失敗は
# 「n が足りても的中が足りない」ことだったので、n を増やして的中を
# 満たすのは正しい方向。
#
# **馬単は 300 のまま据え置く。** 2026-09-30 に判定済み。
MIN_N_BY_KIND = {
    "wide": 500,
}


def min_n(kind: str) -> int:
    """券種の最低 n。定めていない券種は MIN_N。"""
    return MIN_N_BY_KIND.get(kind, MIN_N)


# **券種ごとの最低的中数（#165）。** n だけでは検出力を担保できない。
#
# #56 の馬単はこうなった:
#
#   判定時（9/30）  n=367  的中 1 件  回収率 107.36%  → 「効果あり」
#   5 日後（10/5）  n=806  的中 1 件  回収率  48.88%
#   10/14          n=1788  的中 4 件  回収率  43.09%
#
# **n が 5 倍・的中が 4 倍に増えて 43% に収束した。** あの「効果あり」は
# 的中 1 件が出た直後を切り取った値でしかなかった。n=300 は行数の基準で
# あって検出力の基準ではなかった。
#
# ワイドは「的中>=10 件」も要件にする（kaz 判断・2026-10-14）。
# #169 で分布を引き直した後は n=500 で期待的中 10.2 件になる。
#
# **馬単は 0 のまま据え置く。** 2026-09-30 に判定済みなので、遡って基準を
# 変えると「後から都合よく動かした」ことになる（#56 の作法に反する）。
# 判定結果の弱さは #56 のコメントに記録してある。
#
# #106 / #148 / #56 とは基準が変わるので横並びの比較はできない。
# **比較可能性より結論の信頼性を取った。**
MIN_HITS = {
    "umatan": 0,    # 判定済み（2026-09-30）。遡って変えない
    "wide": 10,
}


def min_hits(kind: str) -> int:
    """券種の最低的中数。定めていない券種は 0（n だけで判定）。"""
    return MIN_HITS.get(kind, 0)


# **判定は 1 回だけ（#171）。** 作法として書くだけではコードは何度でも判定を
# 打てた。判定結果を S3 に条件付き書き込み（IfNoneMatch="*"）で残し、既に
# あれば新しく計算しない。
JUDGMENT_PREFIX = "judgments/exotic_edge/"

# judgments/ ができる前に判定した券種。**遡って S3 に書くのではなく、ここに
# 固定する**（git の履歴で「後から書き換えていない」ことが示せる）。
# 値は #56 の判定コメント（2026-09-30）の転記。
PRIOR_JUDGMENTS = {
    "umatan": {
        "kind": "umatan", "n": 367, "hits": 1,
        "hit_rate": 0.0027, "payback": 1.0736,
        "required_n": 300, "required_hits": 0,
        "threshold": 1.296,   # exotic_threshold("umatan")
        "verdict": "効果あり",
        "judged_at": "2026-09-30",
        "note": "的中 1 件で作られた値。結論として使えない（#56）",
    },
}


def judgment_key(kind: str) -> str:
    return f"{JUDGMENT_PREFIX}{kind}.json"


def _error_code(e) -> str:
    return (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")


def load_judgment(bucket: str, kind: str) -> dict | None:
    """保存済みの判定。無ければ None。"""
    if kind in PRIOR_JUDGMENTS:
        return PRIOR_JUDGMENTS[kind]
    try:
        body = _get_s3().get_object(
            Bucket=bucket, Key=judgment_key(kind))["Body"].read()
    except Exception as e:
        if _error_code(e) in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(body)


def save_judgment(bucket: str, kind: str, record: dict) -> dict | None:
    """判定を 1 回だけ書く。書けたら None、先客がいればその判定を返す。

    **先に読んでから書くだけでは足りない。** 読んだ後に別の実行が書く隙間が
    残るので、書き込み自体を条件付きにする。
    """
    try:
        _get_s3().put_object(
            Bucket=bucket, Key=judgment_key(kind),
            Body=json.dumps(record, ensure_ascii=False, indent=2).encode(),
            ContentType="application/json",
            IfNoneMatch="*")
    except Exception as e:
        if _error_code(e) in ("PreconditionFailed", "412",
                              "ConditionalRequestConflict"):
            return load_judgment(bucket, kind)
        raise
    return None


def _commit() -> str | None:
    """判定したコードの commit。取れなければ None（判定は止めない）。

    **未コミットの変更があれば `-dirty` が付く。** 手元で基準を書き換えて
    判定した場合に、記録から分かるようにするため。
    """
    import subprocess
    try:
        return subprocess.run(
            ["git", "describe", "--always", "--dirty", "--abbrev=40"],
            capture_output=True, text=True, cwd=os.path.dirname(__file__), timeout=5,
            check=True).stdout.strip() or None
    except Exception:
        return None

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def load_rows(bucket: str, kind: str) -> list[dict]:
    """exotic_edge/*.json を全部読み、判定に使える行だけ返す。

    **hit が None の行は落とす。** 着順が足りなかったレース（取消・中止）で、
    外れではない。数えると分母だけ増えて回収率が不当に下がる。

    **いまの閾値を下回る行も落とす（#169）。** 閾値を引き直したとき、
    旧閾値で記録された行が残っている。それを混ぜると「先に決めた閾値で
    記録した」ことにならない:

        旧閾値 1.8555 で記録  568 点
        新閾値 1.9315 以上     459 点  ← 判定に使えるのはこちら

    **記録そのものは壊れていない。** 壊れていたのは分布測定ツールの側
    （記録と違う単勝スナップショットを見ていた）。記録済みの edge 値は
    修正後のツールの再計算と 558 点すべて一致した。だから破棄ではなく
    「閾値で絞る」で足りる。
    """
    s3 = _get_s3()
    thr = exotic_threshold(kind)
    rows = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": "exotic_edge/"}
        if token:
            kw["ContinuationToken"] = token
        res = s3.list_objects_v2(**kw)
        for obj in res.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            for r in json.loads(body).get("rows", []):
                if r.get("kind") != kind:
                    continue
                if r.get("hit") is None:
                    continue
                if thr is not None:
                    e = r.get("edge")
                    if e is None or float(e) < thr:
                        continue
                rows.append(r)
        if not res.get("IsTruncated"):
            break
        token = res.get("NextContinuationToken")
    return rows


def tally(rows: list[dict]) -> dict:
    """的中数と回収率。組合せオッズはそのまま払戻倍率。"""
    n = len(rows)
    hits = [r for r in rows if r.get("hit")]
    payout = sum(float(r.get("odds") or 0) for r in hits)
    return {
        "n": n,
        "hits": len(hits),
        "hit_rate": len(hits) / n if n else 0.0,
        "payback": payout / n if n else 0.0,
    }


def verdict(t: dict, kind: str | None = None) -> str:
    """基準に照らして倒す。100% を跨いだら「効果なし」。

    **n と的中数の両方を要件にする（#165）。** どちらが足りないかを
    区別して返す。的中が足りないまま回収率を読むと、1 件の当落で
    結論が振れる（#56 がそうなった）。

    **n も券種ごと（#169）。** ワイドは 500（分布を引き直してオッズ帯が
    上がり、300 では的中数が足りなくなったため）。

    kind を渡さない場合は既定の MIN_N を使い、的中数は見ない（後方互換）。
    """
    need_n = min_n(kind) if kind else MIN_N
    if t["n"] < need_n:
        return f"判定不可（n {t['n']} < {need_n}）"
    need = min_hits(kind) if kind else 0
    if t["hits"] < need:
        return f"判定不可（的中 {t['hits']} 件 < {need} 件）"
    return "効果あり" if t["payback"] > 1.0 else "効果なし"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kind", required=True,
                    choices=["umatan", "sanrenfuku", "sanrentan",
                             "umafuku", "wide"],
                    help="判定する券種（混ぜない・#56）")
    ap.add_argument("--check", action="store_true",
                    help="n と的中数が足りているかだけ見る（率を出さない）")
    ap.add_argument("--bucket", default=os.environ.get("DATA_BUCKET"))
    a = ap.parse_args(argv)

    if not a.bucket:
        print("DATA_BUCKET を渡すこと", file=sys.stderr)
        return 2

    prior = load_judgment(a.bucket, a.kind)
    if prior and not a.check:
        # **新しく計算しない（#171）。** 計算すれば「いまの値」を見てしまい、
        # 1 回きりの判定が形だけになる。保存済みの判定をそのまま返す
        print(f"{a.kind} は判定済み（{prior.get('judged_at')}）。"
              "再判定しない。保存済みの結果を表示する。", file=sys.stderr)
        print(json.dumps(prior, ensure_ascii=False, indent=2))
        return 0

    rows = load_rows(a.bucket, a.kind)
    t = tally(rows)
    need_n = min_n(a.kind)
    need_hits = min_hits(a.kind)

    if a.check:
        # **的中数も出す（#165）。** 回収率は出さない。到達判定に使うので、
        # 率を見せると「まだ届いていないが数字は見た」状態になる
        print(json.dumps({
            "kind": a.kind,
            "n": t["n"], "required_n": need_n,
            "hits": t["hits"], "required_hits": need_hits,
            "ready": t["n"] >= need_n and t["hits"] >= need_hits,
            "remaining_n": max(0, need_n - t["n"]),
            "remaining_hits": max(0, need_hits - t["hits"]),
            "judged": prior is not None,
        }, ensure_ascii=False, indent=2))
        return 0

    if t["n"] < need_n:
        print(f"n={t['n']} は基準 {need_n} に達していない。判定しない。",
              file=sys.stderr)
        print("n を貯めるか、--check で残数を見ること。", file=sys.stderr)
        return 1

    if t["hits"] < need_hits:
        print(f"的中 {t['hits']} 件は基準 {need_hits} 件に達していない。"
              f"判定しない（#165）。", file=sys.stderr)
        print("n は足りているが検出力が足りない。#56 はこの状態で判定し、"
              "的中 1 件の当落で回収率が 107% → 49% に振れた。",
              file=sys.stderr)
        return 1

    record = {
        "kind": a.kind,
        "n": t["n"],
        "hits": t["hits"],
        "hit_rate": round(t["hit_rate"], 4),
        "payback": round(t["payback"], 4),
        "required_n": need_n,
        "required_hits": need_hits,
        "threshold": exotic_threshold(a.kind),
        "verdict": verdict(t, a.kind),
        "judged_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "commit": _commit(),
    }
    # **書けてから表示する（#171）。** 書けずに表示すると、記録の無い判定を
    # 見たことになり、次の実行でもう 1 回打ててしまう
    other = save_judgment(a.bucket, a.kind, record)
    if other is not None:
        print(f"{a.kind} は別の実行が先に判定した。そちらを表示する。",
              file=sys.stderr)
        record = other
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
