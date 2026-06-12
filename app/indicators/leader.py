"""F 리더 선별 (§9) — 상대강도 + 52주고점 + 정배열 + 200일선 상승."""
from __future__ import annotations

import pandas as pd


def compute_leader_flags(daily_asof: pd.DataFrame, config: dict) -> pd.DataFrame:
    cfg = config["leader"]
    d = daily_asof.copy()
    # 상대강도: 유니버스 내 6-1M 모멘텀 백분위(0~100)
    d["rs_rank"] = d["mom_6m_1m"].rank(pct=True) * 100
    d["is_ma_aligned"] = (d["close"] > d["ma50"]) & (d["ma50"] > d["ma150"]) & (d["ma150"] > d["ma200"])
    d["is_f_leader"] = (
        (d["rs_rank"] >= cfg["rs_threshold"])
        & (d["high_52w_ratio"] >= cfg["high_52w_threshold"])
        & d["is_ma_aligned"].fillna(False)
        & d["ma200_up"].fillna(False)
    )
    return d
