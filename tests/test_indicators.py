"""§21.1 지표 검증 — 일봉지표·20주선 눌림·D4 노출."""
import numpy as np
import pandas as pd
import yaml

from app.indicators.daily import add_daily_indicators
from app.indicators.weekly import add_weekly_indicators
from app.indicators.regime import compute_d4_exposure

CFG = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))


def test_daily_indicators_monotonic():
    n = 260
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = pd.Series(np.linspace(100, 200, n))   # 단조 상승
    d = pd.DataFrame({"date": dates, "ticker": "X", "open": close, "high": close * 1.01,
                      "low": close * 0.99, "close": close, "volume": 1000, "trdval": close * 1000})
    out = add_daily_indicators(d)
    last = out.iloc[-1]
    assert last["close"] > last["ma20"] > last["ma200"]      # 정배열(상승추세)
    assert bool(last["ma200_up"]) is True                    # 200일선 상승
    assert 0.95 <= last["high_52w_ratio"] <= 1.0             # 신고가 근처
    assert last["swing_low20"] == out["low"].iloc[-20:].min()


def test_pullback_flag():
    flat = [100.0] * 24 + [103.0]            # 25주, w_ma20 ≈ 100
    low = [100.0] * 24 + [102.0]             # 마지막 주 저가 102 (≤ ma20×1.05)
    w = pd.DataFrame({"ticker": "X", "week_end": pd.bdate_range("2025-01-03", periods=25, freq="W-FRI"),
                      "close": flat, "low": low, "high": [c * 1.02 for c in flat],
                      "open": flat, "volume": 1, "trdval": 1})
    out = add_weekly_indicators(w, weekly_ma=20, low_band=1.05)
    assert bool(out.iloc[-1]["pullback_20w_105"]) is True    # 저가 눌림 + 종가 위
    # 종가가 20주선 아래면 눌림 아님
    w2 = w.copy(); w2.loc[w2.index[-1], "close"] = 98.0
    out2 = add_weekly_indicators(w2, 20, 1.05)
    assert bool(out2.iloc[-1]["pullback_20w_105"]) is False


def _index(calm: bool):
    n = 160
    dates = pd.bdate_range("2023-01-06", periods=n, freq="W-FRI")
    base = 100 + np.arange(n) * 0.1
    if not calm:
        base = base.astype(float)
        base[-15:] += np.array([(-1) ** i * 12 for i in range(15)])   # 최근 고변동성
    rows = []
    for mk in ("KOSPI", "KOSDAQ"):
        rows.append(pd.DataFrame({"date": dates, "market": mk, "close": base}))
    return pd.concat(rows, ignore_index=True)


def test_d4_calm_vs_stormy():
    asof = pd.Timestamp("2026-01-30")
    calm = compute_d4_exposure(_index(True), asof, CFG)
    stormy = compute_d4_exposure(_index(False), asof, CFG)
    assert calm["target_exposure"] >= stormy["target_exposure"]   # 잔잔하면 노출↑
    assert stormy["target_exposure"] <= 0.7                       # 폭풍이면 디리스킹
    assert calm["mode"] in ("Risk-On", "Half") and "kospi" in calm
