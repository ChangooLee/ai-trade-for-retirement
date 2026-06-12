"""20주선 눌림 (§10) — 검증된 주봉 정의. weekly_asof(완성된 주봉)에서 평가."""
from __future__ import annotations

import pandas as pd


def compute_pullback_flags(weekly_asof: pd.DataFrame, config: dict) -> pd.DataFrame:
    w = weekly_asof.copy()
    # add_weekly_indicators가 이미 pullback_20w_105, dist_wma20를 계산.
    if "pullback_20w_105" not in w.columns:
        cfg = config["pullback"]
        w["pullback_20w_105"] = (w["low"] <= w["w_ma20"] * cfg["low_band"]) & (w["close"] > w["w_ma20"])
        w["dist_wma20"] = w["close"] / w["w_ma20"] - 1
    return w[["ticker", "week_end", "w_ma20", "w_ma5", "w_ma60",
              "dist_wma20", "pullback_20w_105", "low", "close"]].rename(
                  columns={"low": "w_low", "close": "w_close"})
