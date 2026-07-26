"""支持率系の指標計算。fetch（書き込み時の前計算）と api（フォールバック）で共用する。"""
import math


def support_metrics(odds: list) -> tuple[float, float] | None:
    """オッズ列から (top1, ent) を返す。計算不能（全 None/0）なら None。

    top1 = 1 番人気の支持率、ent = 支持率分布の正規化エントロピー（混戦度）。
    """
    inv = [(1.0 / float(o) if o else 0.0) for o in odds]
    s = sum(inv)
    if s <= 0:
        return None
    p = sorted((x / s for x in inv), reverse=True)
    ent = -sum(x * math.log(x) for x in p if x > 0)
    ent_norm = ent / math.log(len(p)) if len(p) > 1 else 0.0
    return round(p[0], 3), round(ent_norm, 3)
