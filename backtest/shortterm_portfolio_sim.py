"""단타 전략 포트폴리오 시뮬레이션 — 신호 통계를 '실제 돈'으로 환산.

전략 A 오버나이트: 거래량 3배 + 종가 +3% 양봉 → 당일 종가 매수, 익일 시가 매도(1박).
        제한: 일 최대 K종목(거래대금 상위), 신호 없는 날 현금. 상한가 잠김(+29.5%↑) 매수 불가 제외.
전략 B 단기반전: 3일 수익률 횡단면 하위 10% & 종가>MA200 → 익일 시가 매수, 3거래일 후 시가 매도.
        3개 트랜치(자본 1/3씩 일별 분할)로 중첩 없이 운용.
비용: 왕복 0.35% (체결당). 생존편향 캐비엇 동일 — KOSPI 대비 상대 성과로 해석.

사용: python -m backtest.shortterm_portfolio_sim [--panel top|broad] [--k 5]
"""
from __future__ import annotations
import argparse, math
import numpy as np, pandas as pd

COST = 0.0035


def load(which):
    import yaml
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    path = cfg["paths"]["daily_ohlcv"] if which == "top" else "data/cache/broad_ohlcv.parquet"
    df = pd.read_parquet(path); df["date"] = pd.to_datetime(df["date"])
    P = {}
    for c in ("open", "close", "volume", "trdval"):
        m = df.pivot_table(index="date", columns="ticker", values=c)
        if c in ("open", "close"):
            m = m.where(m > 0)
        P[c] = m
    return P


def perf(eq, label):
    eq = eq.dropna()
    if len(eq) < 10: return
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    r = eq.pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    print(f"  {label:<26s} 누적 {eq.iloc[-1]/eq.iloc[0]-1:+8.1%} · CAGR {cagr:+6.1%} · MDD {dd:+6.1%} · Sharpe {shp:4.2f}")
    for yr, g in eq.groupby(eq.index.year):
        print(f"      {yr}: {g.iloc[-1]/g.iloc[0]-1:+7.1%}", end="")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", choices=["top", "broad"], default="top")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    P = load(args.panel)
    o, c, v, tv = P["open"], P["close"], P["volume"], P["trdval"]
    ret1 = c.pct_change()
    vol20 = v.rolling(20).mean()
    liquid = tv.rolling(20).mean() > 1e9
    print(f"=== 패널 {args.panel} ({o.index[0].date()}~{o.index[-1].date()}) · K={args.k} · 비용 왕복 {COST*100:.2f}% ===")

    # ---- A: 오버나이트 (종가 매수 → 익일 시가 매도) ----
    sig = (v > 3 * vol20) & (ret1 > 0.03) & (ret1 < 0.295) & liquid   # 상한가 잠김 제외
    r_on = o.shift(-1) / c - 1                                        # 신호일 종가→익일 시가
    rank = tv.where(sig).rank(axis=1, ascending=False)
    picked = rank <= args.k
    day_ret = r_on.where(picked).mean(axis=1)                         # 동일가중
    n_sig = picked.sum(axis=1)
    strat = (day_ret - COST).where(n_sig > 0, 0.0).fillna(0.0)        # 신호 없으면 현금
    eqA = (1 + strat).cumprod()
    eqA.index = o.index
    util = (n_sig > 0).mean()
    print(f"\n[A] 거래량폭발 오버나이트 (1박): 신호일 비율 {util:.0%} · 평균 {n_sig[n_sig>0].mean():.1f}종목/일")
    perf(eqA, f"오버나이트 K={args.k}")

    # ---- B: 단기반전 3일 보유 (3트랜치) ----
    ret3 = c.pct_change(3)
    ma200 = c.rolling(200, min_periods=120).mean()
    sigB = (ret3.rank(axis=1, pct=True) < 0.10) & (c > ma200) & liquid
    rankB = tv.where(sigB).rank(axis=1, ascending=False)
    pickB = rankB <= max(args.k * 2, 10)
    r3 = o.shift(-4) / o.shift(-1) - 1                                # open(i+1)→open(i+4)
    dmB = r3.where(pickB).mean(axis=1)
    print(f"\n[B] 단기반전 3일보유 (3트랜치): 신호일 비율 {(pickB.sum(axis=1)>0).mean():.0%}")
    eqs = []
    for off in range(3):                                              # 비중첩 트랜치
        s = dmB.iloc[off::3].dropna()
        eqs.append((1 + (s - COST).clip(lower=-0.95)).cumprod())
    terminal = float(np.mean([e.iloc[-1] for e in eqs if len(e)]))
    rep = max(eqs, key=len)
    rep = rep / rep.iloc[0] * 1.0
    perf(rep, "반전 트랜치(대표)")
    print(f"      3트랜치 평균 최종배수: {terminal:.2f}x")

    # ---- 비교: KOSPI proxy(유니버스 동일가중 보유) ----
    ew = c.pct_change().mean(axis=1)
    perf((1 + ew.fillna(0)).cumprod(), "유니버스 동일가중(기준선)")


if __name__ == "__main__":
    main()
