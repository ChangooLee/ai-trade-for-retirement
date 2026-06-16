"""검증A — 검증된 모멘텀(+155%)의 실집행 슬리피지 실측.
+155% 백테스트는 '익일 시가(daily open)' 체결을 가정(R1: 종가체결 시 CAGR 반토막).
질문: 그 시가 체결이 현실에서 달성 가능한가? 슬리피지가 엣지를 갉아먹나?
방법: 전략의 최근 1년 실제 진입종목(분봉 존재 구간)만 표적 분봉 조회 →
  가정체결가(daily open) vs 현실체결가(MOO=첫분 시가 / 시초후 첫5·15분 VWAP) 슬리피지 분포.
  ★분봉 1년 보관이라 2017~2024 진입은 측정 불가 — 최근창 분포로 전체비용 보정만 가능(KIS 한계).★
사용: python -m backtest.verify_a_slippage
"""
from __future__ import annotations
import sys
import numpy as np, pandas as pd
sys.path.insert(0, ".")
from app.data import kis_api as K  # noqa: E402

rec = pd.read_csv("/tmp/mom_recent_entries.csv", dtype={"entry_date": str, "ticker": str})
mn = pd.read_parquet("data/cache/minute_ohlcv.parquet")
mn["date"] = mn["date"].astype(str); mn["ticker"] = mn["ticker"].astype(str)
have = {(t, d): g.sort_values("time") for (t, d), g in mn.groupby(["ticker", "date"])}

rows = []
for _, r in rec.iterrows():
    tk, d, assumed = r["ticker"], r["entry_date"], float(r["assumed_open"])
    bars = have.get((tk, d))
    if bars is None:                                     # 표적 백필(최근창 진입종목)
        try:
            b = K.get_minute_bars(tk, d)
            bars = pd.DataFrame(b).sort_values("time") if b else None
        except Exception as e:
            print(f"  {tk} {d} 분봉 실패: {str(e)[:60]}", file=sys.stderr); bars = None
    if bars is None or len(bars) == 0:
        continue
    bars = bars.reset_index(drop=True)
    o = float(bars.iloc[0]["open"])                      # 첫 1분봉 시가 ≈ 시초가(MOO 체결 근사)
    c1 = float(bars.iloc[0]["close"])                    # 첫 1분 종가(시초직후 시장가 근사)
    def vwap(k):
        s = bars.head(k)
        v = s["vol"].sum()
        return float((s["close"] * s["vol"]).sum() / v) if v > 0 else float(s["close"].mean())
    rows.append({"ticker": tk, "date": d, "assumed": assumed, "min_open": o,
                 "moo_slip": o / assumed - 1 if assumed else 0,        # daily open vs 분봉 시초가(정합성)
                 "c1_slip": c1 / assumed - 1 if assumed else 0,        # 첫1분 종가 체결
                 "vwap5_slip": vwap(5) / assumed - 1 if assumed else 0,
                 "vwap15_slip": vwap(15) / assumed - 1 if assumed else 0})

df = pd.DataFrame(rows)
print(f"검증A 슬리피지 실측: {len(df)}/{len(rec)}건 측정(분봉 존재) · {df['ticker'].nunique()}종목")
print("★최근 1년창·생존종목 — 전체 2017~ 소급 불가, 비용보정 근거용★\n")
def stat(col, label):
    v = df[col].dropna() * 100
    print(f"  {label:<28} 중앙값 {v.median():+.3f}%  평균 {v.mean():+.3f}%  "
          f"p90 {v.quantile(.9):+.3f}%  최대 {v.max():+.3f}%")
print("진입 슬리피지(가정 daily-open 대비, +면 더 비싸게 체결=손해):")
stat("moo_slip", "분봉시초가(MOO 근사)")
stat("c1_slip", "첫1분 종가(시초후 시장가)")
stat("vwap5_slip", "첫5분 VWAP")
stat("vwap15_slip", "첫15분 VWAP")
df.to_csv("/tmp/verify_a_slippage.csv", index=False)
print(f"\n→ 왕복 비용보정 권장(진입+청산 대칭 가정, 보수적): "
      f"2×첫5분VWAP중앙값 = {2*df['vwap5_slip'].median()*100:+.3f}%p")
print("  (이 값을 pit_mktcap_backtest --cost-add 로 부과해 +155% 잔존 확인)")
