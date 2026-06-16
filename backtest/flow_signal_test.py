"""R6 예비검증 — 외국인+기관 순매수(수급)가 forward 수익을 예측하나? (횡단면 IC + 분위 스프레드)
★29일·단일국면·생존종목 — 예비 신호확인용. 통계 결론 아님(본검증은 수개월 누적 후).★
사용: python -m backtest.flow_signal_test
"""
from __future__ import annotations
import sys
import numpy as np, pandas as pd, yaml
sys.path.insert(0, ".")
from app.data import krx_loader as L  # noqa: E402

cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
flow = pd.read_parquet("data/cache/investor_flow.parquet"); flow["date"] = pd.to_datetime(flow["date"])
daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
cls = daily.pivot_table(index="date", columns="ticker", values="close").where(lambda x: x > 0)
tv = daily.pivot_table(index="date", columns="ticker", values="trdval")
tv20 = tv.rolling(20).mean()

flow["fi"] = flow["frgn_ntby"].fillna(0) + flow["orgn_ntby"].fillna(0)   # 외국인+기관 순매수 거래대금
fi = flow.pivot_table(index="date", columns="ticker", values="fi")
dates = sorted(set(fi.index) & set(cls.index))
fi = fi.reindex(dates)

# forward 수익 (close→close), 신호일 d 기준 d→d+1, d→d+5
fwd1 = (cls.shift(-1) / cls - 1).reindex(dates)
fwd5 = (cls.shift(-5) / cls - 1).reindex(dates)
norm = (fi / tv20.reindex(dates)).replace([np.inf, -np.inf], np.nan)     # 유동성 정규화(순매수/20일평균거래대금)

def ic(sig, fwd):
    vals = []
    for d in dates:
        s, f = sig.loc[d], fwd.loc[d]
        m = s.notna() & f.notna()
        if m.sum() >= 30:
            vals.append(s[m].rank().corr(f[m].rank()))   # 횡단면 Spearman IC
    v = np.array([x for x in vals if x == x])
    t = v.mean() / v.std() * np.sqrt(len(v)) if len(v) > 1 and v.std() > 0 else 0
    return v.mean(), t, len(v)

def quintile(sig, fwd):
    diffs = []
    for d in dates:
        s, f = sig.loc[d], fwd.loc[d]; m = s.notna() & f.notna()
        if m.sum() >= 30:
            s, f = s[m], f[m]; q = s.rank(pct=True)
            top = f[q > 0.8].mean(); bot = f[q < 0.2].mean()
            if top == top and bot == bot: diffs.append(top - bot)
    d = np.array(diffs)
    return d.mean(), (d.mean() / d.std() * np.sqrt(len(d)) if len(d) > 1 and d.std() > 0 else 0), len(d)

print(f"수급 예비검증: {fi.notna().sum().sum():,} 관측 · {len(dates)}일 · {fi.shape[1]}종목")
print("★29일·단일국면·생존종목 — 예비신호용, 통계결론 아님★\n")
print(f"{'신호':<28}{'IC(1d) 평균/t':>18}{'IC(5d) 평균/t':>18}")
for name, sig in [("외인+기관 순매수(raw)", fi), ("순매수/20일거래대금(정규화)", norm)]:
    i1 = ic(sig, fwd1); i5 = ic(sig, fwd5)
    print(f"{name:<26}{i1[0]:>+8.3f}/{i1[1]:>5.2f}{'':>4}{i5[0]:>+8.3f}/{i5[1]:>5.2f}")
print(f"\n분위 스프레드(상위20% − 하위20% 수급, 익일수익):")
q1 = quintile(fi, fwd1); q5 = quintile(fi, fwd5)
print(f"  1일: {q1[0]*100:+.3f}%/밤 (t={q1[1]:.2f}, {q1[2]}일) · 5일: {q5[0]*100:+.3f}% (t={q5[1]:.2f})")
print("\n※ IC≈0이면 수급에 예측력 없음. +유의해도 29일·생존·강세장이라 본검증은 수개월 누적 후.")
