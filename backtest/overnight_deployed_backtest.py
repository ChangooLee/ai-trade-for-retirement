"""배포된 오버나이트 단타 룰 그대로의 포트폴리오 백테스트.

화면 룰 재현:
  신호  당일 거래량 > 20일평균×3 & 등락 +3%~+28.5%(상한가 잠김 제외) & 20일 평균 거래대금 10억+
  선정  거래대금 상위 K=8 (화면 표시 개수와 동일), 동일가중
  집행  신호일 종가 매수 → 익일 시가 전량 매도(가격 불문), 왕복 0.35%
  노출  총자산의 30% (화면 권장) — 100% 비교 병기
  게이트 D4 시장국면: 화면은 Risk-Off 시 '자제 권장' → 게이트 적용/미적용 비교로 권고 타당성 검증

캐비엇: 생존편향 유니버스(절대수치 낙관), 체결가=종가/시가 정확 체결 가정(급등주 슬리피지 미반영).
사용: python -m backtest.overnight_deployed_backtest
"""
from __future__ import annotations
import math, sys
import numpy as np, pandas as pd, yaml

sys.path.insert(0, ".")
from app.data import krx_loader as L  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402

COST = 0.0035
K = 8


def perf(eq, label):
    eq = eq.dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    r = eq.pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    print(f"  {label:<34s} 누적 {eq.iloc[-1]/eq.iloc[0]-1:+9.1%} · CAGR {cagr:+6.1%} · MDD {dd:+6.1%} · Sharpe {shp:5.2f}")
    return eq


def main():
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
    index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    P = {c: daily.pivot_table(index="date", columns="ticker", values=c)
         for c in ("open", "close", "volume", "trdval")}
    for c in ("open", "close"):
        P[c] = P[c].where(P[c] > 0)
    o, c_, v, tv = P["open"], P["close"], P["volume"], P["trdval"]
    ret1 = c_.pct_change()
    vol20 = v.rolling(20).mean()
    liq = tv.rolling(20).mean() > 1e9
    sig = (v > 3 * vol20) & (ret1 > 0.03) & (ret1 < 0.285) & liq
    picked = tv.where(sig).rank(axis=1, ascending=False) <= K
    r_on = (o.shift(-1) / c_ - 1).where(picked)
    night = r_on.mean(axis=1)                       # 신호 밤의 동일가중 수익(gross)
    npicks = picked.sum(axis=1)
    dates = night.index

    # D4 국면 (주 단위 캐시 — 지수는 주봉)
    print("D4 국면 계산 중...", file=sys.stderr)
    wk_keys = pd.Series(dates, index=dates).dt.to_period("W").astype(str)
    cache = {}
    risk_on = []
    for d, wkey in zip(dates, wk_keys):
        if wkey not in cache:
            try:
                cache[wkey] = compute_d4_exposure(index[index["date"] <= d], d, cfg)["mode"]
            except Exception:
                cache[wkey] = "Risk-On"
        risk_on.append(cache[wkey] != "Risk-Off")
    risk_on = pd.Series(risk_on, index=dates)

    start = dates[dates >= dates[0] + pd.Timedelta(days=400)][0]   # 지표 워밍업 이후
    sl = slice(start, None)
    base = night.loc[sl]
    print(f"\n=== 배포 룰 그대로 ({start.date()}~{dates[-1].date()}, K={K}, 비용 {COST*100:.2f}%) ===")
    print(f"  신호 밤 비율 {(npicks.loc[sl]>0).mean():.0%} · 평균 {npicks.loc[sl][npicks.loc[sl]>0].mean():.1f}종목/밤 · "
          f"밤 승률(net) {((base-COST)>0).mean():.0%}")
    rows = {}
    for label, gate in [("게이트 없음", None), ("Risk-Off 밤 휴식(화면 권고)", risk_on.loc[sl])]:
        for expo in (0.30, 1.00):
            strat = (base - COST).fillna(0.0)
            if gate is not None:
                strat = strat.where(gate, 0.0)
            eq = (1 + strat * expo).cumprod()
            rows[f"{label} · 노출 {expo:.0%}"] = perf(eq, f"{label} · 노출 {expo:.0%}")
    # 연도별 (권고안: 게이트+30%)
    strat = ((base - COST).fillna(0.0)).where(risk_on.loc[sl], 0.0) * 0.30
    eq = (1 + strat).cumprod()
    print("\n  [권고안: 게이트 + 노출 30%] 연도별:")
    for yr, g in eq.groupby(eq.index.year):
        print(f"    {yr}: {g.iloc[-1]/g.iloc[0]-1:+6.1%}", end="")
    print()
    g26 = base[base.index.year >= 2025]
    print(f"\n  최근(2025~) 신호 밤 net 평균 {(g26.mean()-COST)*100:+.2f}%/밤 · Risk-Off 게이트로 제외된 밤 비율 "
          f"{(~risk_on.loc[sl]).mean():.0%}")


if __name__ == "__main__":
    main()
