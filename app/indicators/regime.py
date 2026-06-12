"""D4 변동성 목표노출 (§8) — KOSPI/KOSDAQ 시장별 변동성 분위 → 노출, 결합은 min().

현재 지수는 주봉 해상도(검증 일관성). 일봉 vol 전환은 daily index 확보 후.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _market_state(s: pd.DataFrame, asof, cfg) -> dict:
    s = s[s["date"] <= asof].sort_values("date")
    close = s["close"]
    ret = close.pct_change()
    vol = ret.rolling(cfg["vol_window_weeks"]).std() * np.sqrt(52)
    vol_pct = vol.rolling(cfg["vol_percentile_window_weeks"], min_periods=52).rank(pct=True)
    ma = close.rolling(cfg["ma_weeks"], min_periods=20).mean()
    if len(close) == 0:
        return dict(close=np.nan, ma40w=np.nan, above_40w=False, vol=np.nan,
                    vol_percentile=np.nan, mode="Risk-Off", exposure=cfg["risk_off_exposure"])
    vp = float(vol_pct.iloc[-1]) if pd.notna(vol_pct.iloc[-1]) else 0.5
    if vp <= cfg["risk_on_percentile"]:
        mode, exp = "Risk-On", cfg["risk_on_exposure"]
    elif vp <= cfg["half_percentile"]:
        mode, exp = "Half", cfg["half_exposure"]
    else:
        mode, exp = "Risk-Off", cfg["risk_off_exposure"]
    return dict(close=float(close.iloc[-1]), ma40w=float(ma.iloc[-1]) if pd.notna(ma.iloc[-1]) else np.nan,
                above_40w=bool(close.iloc[-1] > ma.iloc[-1]) if pd.notna(ma.iloc[-1]) else False,
                vol=float(vol.iloc[-1]) if pd.notna(vol.iloc[-1]) else np.nan,
                vol_percentile=vp, mode=mode, exposure=exp)


def compute_d4_exposure(index_df: pd.DataFrame, asof, config: dict) -> dict:
    cfg = config["exposure"]
    kospi = _market_state(index_df[index_df.market == "KOSPI"], asof, cfg)
    kosdaq = _market_state(index_df[index_df.market == "KOSDAQ"], asof, cfg)
    if cfg.get("combine_market_exposure", "min") == "min":
        exposure = min(kospi["exposure"], kosdaq["exposure"])
    else:
        exposure = 0.5 * kospi["exposure"] + 0.5 * kosdaq["exposure"]
    mode = "Risk-On" if exposure >= 0.95 else ("Half" if exposure >= 0.6 else "Risk-Off")
    return {"mode": mode, "target_exposure": round(exposure, 4), "cash": round(1 - exposure, 4),
            "kospi": kospi, "kosdaq": kosdaq}
