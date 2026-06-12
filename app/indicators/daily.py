"""일봉 지표 (§6) — 이동평균·거래대금평균·모멘텀·52주 고점."""
from __future__ import annotations

import pandas as pd


def add_daily_indicators(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.sort_values(["ticker", "date"]).copy()
    g = d.groupby("ticker", group_keys=False)
    for w in (5, 20, 50, 60, 120, 150, 200):
        d[f"ma{w}"] = g["close"].transform(lambda s, w=w: s.rolling(w).mean())
    d["avg_trdval20"] = g["trdval"].transform(lambda s: s.rolling(20).mean())
    d["avg_trdval60"] = g["trdval"].transform(lambda s: s.rolling(60).mean())
    d["ma200_up"] = d.groupby("ticker")["ma200"].transform(lambda s: s > s.shift(20))
    d["mom_6m_1m"] = g["close"].transform(lambda s: s.shift(21) / s.shift(126) - 1)
    d["high_52w"] = g["high"].transform(lambda s: s.rolling(252).max())
    d["high_52w_ratio"] = d["close"] / d["high_52w"]
    d["swing_low20"] = g["low"].transform(lambda s: s.rolling(20).min())   # 참고위험선
    d["listing_days"] = g["close"].transform("cumcount") + 1
    return d
