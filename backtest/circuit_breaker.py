"""서킷브레이커(손실 한도) 검증 — 복합 80/20 일수익에 손실 차단 규칙을 씌워 효과 측정.

목적: 사용자 1순위 원칙('절대 잃지 않기')을 운영 규칙으로 구현. MDD를 한도로 묶되 수익 손실을 정량화.
입력: state/sleeve_returns.csv (composite_strategy_backtest.py --save 산출)
규칙:
  none      차단 없음(기준)
  monthly   당월 누적손실 ≤ -L% 도달 시 그달 잔여 휴식(현금)
  trailing  고점 대비 낙폭 ≤ -D% 도달 시, 고점 대비 -R% 이내 회복까지 휴식
각 규칙의 CAGR/MDD/Sharpe/최악월/현금기간 비율 보고.

사용: python -m backtest.circuit_breaker [--alloc-long 0.8]
"""
from __future__ import annotations
import argparse, math
import numpy as np, pandas as pd


def stats(r, cap0=1.0):
    eq = (1 + r).cumprod() * cap0
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if cap0 == 1 else (eq.iloc[-1] / cap0) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    worst_m = r.resample("ME").apply(lambda x: (1 + x).prod() - 1).min()
    return cagr, dd, shp, worst_m


def monthly_breaker(r, L):
    out = r.copy(); active = True; mkey = None; mtd = 0.0
    for d in r.index:
        k = (d.year, d.month)
        if k != mkey:
            mkey, mtd, active = k, 0.0, True
        if not active:
            out[d] = 0.0
            continue
        mtd = (1 + mtd) * (1 + r[d]) - 1
        if mtd <= -L:
            active = False                 # 이날까지 손실 반영 후 잔여 휴식
    return out


def trailing_breaker(r, D, R):
    out = r.copy(); eq = 1.0; peak = 1.0; halted = False
    for d in r.index:
        if halted:
            out[d] = 0.0
            if eq >= peak * (1 - R):       # 고점 대비 -R% 이내 회복 시 재개
                halted = False
            continue
        eq *= (1 + r[d]); peak = max(peak, eq)
        if eq <= peak * (1 - D):
            halted = True
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alloc-long", type=float, default=0.8)
    args = ap.parse_args()
    df = pd.read_csv("state/sleeve_returns.csv", index_col=0, parse_dates=True)
    aL = args.alloc_long
    base = (aL * df["long"].fillna(0) + (1 - aL) * df["overnight"].fillna(0))
    print(f"=== 서킷브레이커 (복합 장기{aL:.0%}/단타{1-aL:.0%}, PIT {base.index[0].date()}~{base.index[-1].date()}) ===")
    print(f"  {'규칙':<26s}{'CAGR':>8s}{'MDD':>8s}{'Sharpe':>8s}{'최악월':>8s}{'현금기간':>8s}")
    rules = [("없음(기준)", base)]
    for L in (0.03, 0.05, 0.08):
        rules.append((f"월손실한도 -{int(L*100)}%", monthly_breaker(base, L)))
    for D, R in ((0.10, 0.03), (0.15, 0.05), (0.20, 0.05)):
        rules.append((f"낙폭차단 -{int(D*100)}%→-{int(R*100)}%복귀", trailing_breaker(base, D, R)))
    for name, r in rules:
        cagr, dd, shp, wm = stats(r)
        cashpct = (r == 0).mean()
        print(f"  {name:<26s}{cagr*100:+7.1f}%{dd*100:+7.1f}%{shp:8.2f}{wm*100:+7.1f}%{cashpct*100:7.0f}%")
    print("  ※ 목표: MDD를 한도로 묶되 CAGR 손실 최소. 낙폭차단은 휘프소(바닥서 나가 회복 놓침) 위험.")


if __name__ == "__main__":
    main()
