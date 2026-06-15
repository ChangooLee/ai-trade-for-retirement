"""1분봉 단타 백테스트 부활 — KIS 1분봉(minute_ohlcv.parquet)으로 장중 전략 검증.
EOD-only로는 불가능했던 1분 해상도 단타를 실제 가격으로. (소표본 데모 — 깊은 백필 후 본검증.)

전략 A) 오프닝레인지 돌파(ORB): 09:00~09:30 고가 돌파 시 매수 → 15:20 청산.
전략 B) 오버나이트: 15:30 종가 매수 → 익일 09:00 시가 매도(전일 EOD근사 아닌 실제 분봉가).
전략 C) 기준선: 09:00 시가 매수 → 15:20 청산(장중 보유).
비용: 왕복 --cost (기본 0.30%).
사용: python -m backtest.intraday_backtest [--cost 0.003]
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np, pandas as pd

OUT = "data/cache/minute_ohlcv.parquet"


def day_bars(g):
    return g.sort_values("time")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cost", type=float, default=0.003); a = ap.parse_args()
    if not os.path.exists(OUT):
        print("minute_ohlcv.parquet 없음 — 먼저 build_minute 실행"); return
    m = pd.read_parquet(OUT)
    m["time"] = m["time"].astype(str).str.zfill(6)
    print(f"데이터: {m['ticker'].nunique()}종목 · {m['date'].nunique()}일 · {len(m):,}분봉 "
          f"({m['date'].min()}~{m['date'].max()})\n비용 왕복 {a.cost*100:.2f}%\n")

    orb, base, overn = [], [], []
    # A) ORB, C) base — ticker-day 단위
    for (tk, d), g in m.groupby(["ticker", "date"]):
        g = day_bars(g)
        orng = g[g["time"] <= "093000"]
        sess = g[(g["time"] > "093000") & (g["time"] <= "152000")]
        if len(orng) < 5 or len(sess) < 5:
            continue
        or_high = orng["high"].max()
        exit_px = sess.iloc[-1]["close"]
        # ORB
        brk = sess[sess["close"] > or_high]
        if len(brk):
            entry = brk.iloc[0]["close"]
            orb.append(exit_px / entry - 1 - a.cost)
        # base (09:00 시가 → 15:20)
        op = g.iloc[0]["open"]
        if op > 0:
            base.append(exit_px / op - 1 - a.cost)
    # B) 오버나이트 — ticker별 연속일
    for tk, g in m.groupby("ticker"):
        days = sorted(g["date"].unique())
        for i in range(len(days) - 1):
            d0, d1 = days[i], days[i + 1]
            c = g[(g["date"] == d0)].sort_values("time")
            o = g[(g["date"] == d1)].sort_values("time")
            if not len(c) or not len(o):
                continue
            buy = c.iloc[-1]["close"]            # 15:30 종가
            sell = o.iloc[0]["open"]             # 익일 09:00 시가
            if buy > 0 and sell > 0:
                overn.append(sell / buy - 1 - a.cost)

    def rep(name, r):
        if not r:
            print(f"  {name:<26} (표본 없음)"); return
        r = np.array(r)
        print(f"  {name:<26} 거래 {len(r):>4} · 평균 {r.mean()*100:>+6.2f}% · 승률 {(r>0).mean():>4.0%} · "
              f"중앙 {np.median(r)*100:>+5.2f}% · 누적(동일가중합) {r.sum()*100:>+6.1f}%")
    print("전략별 결과(왕복비용 차감):")
    rep("A) 오프닝레인지 돌파", orb)
    rep("B) 오버나이트(15:30→익일09:00)", overn)
    rep("C) 장중보유(09:00→15:20)", base)
    print("\n※ 소표본 데모(13종목×15일) — 1분봉 단타 백테스트 '부활' 확인용. 통계적 결론 아님.")


if __name__ == "__main__":
    main()
