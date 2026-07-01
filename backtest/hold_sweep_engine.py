"""보유기간(시간청산 H) 스윕 — 검증된 엔진(app.sim.backtest_cli.run, engine.execute_day) 재사용.
화면 '기간 백테스트'와 동일 경로 → H=40이 검증치 재현(정합 확인용). cb_limit=0.03·exposure×1 고정.
★아카이브(bt_prices=현 top400 daily_ohlcv)라 생존편향 있음 — 상대비교용. PIT는 별도.★
사용: python -m backtest.hold_sweep_engine
"""
from __future__ import annotations
import math, os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.sim import backtest_cli as B  # noqa: E402


def metrics(out):
    ec = out.get("equity_curve") or []
    if len(ec) < 5:
        return None
    eq = pd.Series([e["equity"] for e in ec], index=pd.to_datetime([e["date"] for e in ec]))
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if eq.iloc[-1] > 0 and yrs > 0 else -1
    r = eq.pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    s = out["summary"]
    return cagr, s["mdd"], shp, s["total_ret"], s["n_trades"], s["win_rate"]


def main():
    for label, start, end in [("2017~2026(전체)", "2017-05-01", "2026-06-30"),
                              ("2022~2026", "2022-01-01", "2026-06-30")]:
        print(f"[{label}]  (검증 엔진 · cb −3% · 노출×1)")
        for H in [40, 60, 80, 100, 120]:
            out = B.run(start, end, 10_000_000, 1.0, 0.03, "block", hold_days_override=H)
            m = metrics(out)
            if not m:
                print(f"  H={H:3d}: (데이터 부족)"); continue
            c, mdd, shp, tot, n, wr = m
            print(f"  H={H:3d}거래일: CAGR {c:+.1%} | MDD {mdd:+.1%} | Sharpe {shp:.2f} | 거래 {n} 승률 {wr:.0%} | 총 {tot:+.0%}")


if __name__ == "__main__":
    main()
