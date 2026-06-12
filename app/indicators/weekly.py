"""주봉 변환 + 주봉 지표 (§5) — week_end = 해당 주 마지막 거래일."""
from __future__ import annotations

import pandas as pd


def to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.sort_values(["ticker", "date"]).copy()
    d["wk"] = d["date"].dt.to_period("W")
    agg = {"date": "last", "open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum", "trdval": "sum",
           "name": "last", "market": "last"}
    wk = d.groupby(["ticker", "wk"], as_index=False).agg(agg)
    wk = wk.rename(columns={"date": "week_end"})
    return wk.sort_values(["ticker", "week_end"])


def add_weekly_indicators(weekly: pd.DataFrame, weekly_ma: int = 20, low_band: float = 1.05) -> pd.DataFrame:
    w = weekly.sort_values(["ticker", "week_end"]).copy()
    g = w.groupby("ticker", group_keys=False)
    w["w_ma5"] = g["close"].transform(lambda s: s.rolling(5).mean())
    w["w_ma20"] = g["close"].transform(lambda s: s.rolling(weekly_ma).mean())
    w["w_ma60"] = g["close"].transform(lambda s: s.rolling(60).mean())
    w["dist_wma20"] = w["close"] / w["w_ma20"] - 1
    # 20주선 눌림: 그 주 저가가 20주선 5% 이내 & 종가는 20주선 위 (검증된 정의)
    w["pullback_20w_105"] = (w["low"] <= w["w_ma20"] * low_band) & (w["close"] > w["w_ma20"])
    return w
