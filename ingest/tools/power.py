"""基準を決める前に検出力を見積もる（#171）。

**判定基準（n・的中数）を固定する前に、この数字を issue に貼る。**

#56 の馬単は「n>=300」だけを決めて判定し、n=367 / 的中 1 件で「効果あり
（107.36%）」が出た。5 日後には同じ的中 1 件のまま 48.88% に落ちた。
n は行数の基準であって、検出力の基準ではなかった。

このツールは「その n で、回収率がどれだけ偶然で振れるか」を先に出す。

## 何を出すか

的中率の見積もり `p` と候補の n から:

- **期待的中数** `n * p`
- **的中が MIN_HITS に届かない確率**
- **真の回収率が r のときに観測される回収率の 95% 区間**
- **偽陽性率** — 真の回収率が `--null-payback`（既定 0.80 = 控除率 20%）なのに、
  観測が 100% を超えて「効果あり」と出る確率

## モデル（楽観的な下限）

的中数だけが振れるとみなす。的中は Binomial(n, p)、当たった組のオッズは
平均 `r / p` で固定（= 真の回収率が r になる）:

    観測回収率 = 的中数 / (n * p) * r

**実際はオッズのばらつきが乗るので、区間はこれより広い。** ここで出る幅は
「最低でもこれだけ振れる」という下限として読む。下限ですら 100% を跨ぐなら、
その n では判定しても読めない。

## p の取り方

**判定前のログ（`exotic_edge/`）から取らない。** 的中率を数えれば回収率も
見えてしまう（#170 はそれで判定前の率を見た）。理論確率の平均や、判定済みの
別券種の実績など、**結果を含まない**材料から見積もる。

## 例: #56 の馬単（判定時の的中率 0.27%）

    $ python -m ingest.tools.power --hit-rate 0.0027 --n 300 1000 5000

          n  期待的中    的中0件  95%区間(r=1.00)  偽陽性(r=0.80)
        300      0.81   44.4%     0% .. 370%        19.5%
       1000      2.70    6.7%     0% .. 222%        28.6%
       5000     13.50    0.0%    52% .. 156%        20.2%

n=300 では 2 回に 1 回近く 1 件も当たらず、区間は 0〜370%。**基準を決める
前にこれが出ていれば、n=300 で判定する設計にはならなかった。**
（偽陽性率が n とともに単調に下がらないのは、的中数が整数で階段状に
動くため。）

使い方:

    python -m ingest.tools.power --hit-rate 0.0204 --n 300 500 --min-hits 10
"""
import argparse
import math


def binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    lg = (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
          + k * math.log(p) + (n - k) * math.log1p(-p))
    return math.exp(lg)


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(H <= k)。"""
    if k < 0:
        return 0.0
    return min(1.0, sum(binom_pmf(i, n, p) for i in range(min(k, n) + 1)))


def binom_quantile(q: float, n: int, p: float) -> int:
    """P(H <= k) >= q となる最小の k。"""
    acc = 0.0
    for k in range(n + 1):
        acc += binom_pmf(k, n, p)
        if acc >= q:
            return k
    return n


def estimate(n: int, p: float, min_hits: int = 0,
             true_payback: float = 1.0, null_payback: float = 0.80) -> dict:
    """1 つの n について検出力の見積もりを返す。"""
    if not 0.0 < p < 1.0:
        raise ValueError(f"hit rate must be in (0, 1): {p}")
    scale = 1.0 / (n * p)
    lo = binom_quantile(0.025, n, p)
    hi = binom_quantile(0.975, n, p)
    # 観測回収率 = H / (n p) * r が 1 を超える = H > n p / r
    cut = math.floor(n * p / null_payback)
    return {
        "n": n,
        "expected_hits": n * p,
        "p_zero_hits": binom_pmf(0, n, p),
        "p_short_hits": binom_cdf(min_hits - 1, n, p),
        "payback_lo": lo * scale * true_payback,
        "payback_hi": hi * scale * true_payback,
        "false_positive": 1.0 - binom_cdf(cut, n, p),
    }


def render(rows: list[dict], min_hits: int, true_payback: float,
           null_payback: float) -> str:
    short = f"的中<{min_hits}" if min_hits else "的中0件"
    L = [f"{'n':>7}  期待的中  {short:>6}  95%区間(r={true_payback:.2f})"
         f"  偽陽性(r={null_payback:.2f})"]
    for r in rows:
        miss = r["p_short_hits"] if min_hits else r["p_zero_hits"]
        L.append(f"{r['n']:>7}  {r['expected_hits']:>8.2f}  {miss:>6.1%}"
                 f"  {r['payback_lo']:>5.0%} .. {r['payback_hi']:<5.0%}"
                 f"      {r['false_positive']:>6.1%}")
    L.append("")
    L.append("区間は的中数の揺れだけを見た下限。オッズのばらつきが乗る分、"
             "実際はもっと広い。")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hit-rate", type=float, required=True,
                    help="的中率の見積もり（判定前のログから取らない）")
    ap.add_argument("--n", type=int, nargs="+", required=True,
                    help="候補の n（複数可）")
    ap.add_argument("--min-hits", type=int, default=0,
                    help="的中数の要件（#165）")
    ap.add_argument("--true-payback", type=float, default=1.0,
                    help="区間を出すときの真の回収率（既定 1.0）")
    ap.add_argument("--null-payback", type=float, default=0.80,
                    help="偽陽性率を出すときの真の回収率（既定 0.80）")
    a = ap.parse_args(argv)
    rows = [estimate(n, a.hit_rate, a.min_hits, a.true_payback,
                     a.null_payback) for n in a.n]
    print(render(rows, a.min_hits, a.true_payback, a.null_payback))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
