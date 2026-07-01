"""정체컷 포트폴리오 스윕 — 검증 엔진(backtest_cli.run, 재투자 포함)으로 자본효율까지 확인.
기준(컷없음) vs 여러 (K,C) 정체컷 → CAGR/MDD/Sharpe. 슬롯 회전 이득이 컷 손실을 상쇄하는지 본다.
사용: python -m backtest.early_cut_sweep
"""
from __future__ import annotations
import math, os, sys
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.sim import backtest_cli as B  # noqa: E402


def metrics(out):
    ec = out.get("equity_curve") or []
    if len(ec) < 5:
        return None
    eq = pd.Series([e["equity"] for e in ec], index=pd.to_datetime([e["date"] for e in ec]))
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if eq.iloc[-1] > 0 and yrs > 0 else -1
    r = eq.pct_change().dropna(); shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    s = out["summary"]
    return cagr, s["mdd"], shp, s["total_ret"], s["n_trades"], s["win_rate"]


def main():
    configs = [("기준(컷없음)", 0, 0.0), ("K10 ret≤+1%", 10, 0.01), ("K15 ret≤+1%", 15, 0.01),
               ("K15 ret≤+3%", 15, 0.03), ("K20 ret≤0%", 20, 0.0), ("K20 ret≤+3%", 20, 0.03)]
    for label, start, end in [("2017~2026", "2017-05-01", "2026-06-30"), ("2022~2026", "2022-01-01", "2026-06-30")]:
        print(f"[{label}] 검증 엔진 · 40일 시간청산 + 정체컷 · cb−3% · 노출×1")
        for name, cd, cr in configs:
            out = B.run(start, end, 10_000_000, 1.0, 0.03, "block", early_cut_days=cd, early_cut_ret=cr)
            m = metrics(out)
            if not m:
                print(f"  {name:14s}: (데이터 부족)"); continue
            c, mdd, shp, tot, n, wr = m
            print(f"  {name:14s}: CAGR {c:+.1%} | MDD {mdd:+.1%} | Sharpe {shp:.2f} | 거래 {n} 승률 {wr:.0%} | 총 {tot:+.0%}")


if __name__ == "__main__":
    main()
