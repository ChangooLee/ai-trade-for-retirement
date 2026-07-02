"""글로벌 ETF 로테이션 백테스트 — '복구 방법론' 검증 (전부 KRX 상장 KRW ETF, 환율 내장, 토스로 집행 가능).

자산: KODEX200(069500)·코스닥150(229200)·TIGER나스닥100(133690)·골드(132030)[·S&P500(360750) 2020~].
규칙(우리 코어 로테이션과 동일 사상): 주간, 200일선 위 자산 중 13주 모멘텀 최상위 1개 보유(전액),
전부 추세 아래면 현금. 왕복비용 0.25%(ETF 스프레드 얇음). 비교: KR-only 로테이션·KOSPI 보유·개별 보유.
사용: .venv/bin/python -m backtest.global_rotation_backtest [--start 2016-01-01]
"""
from __future__ import annotations
import argparse, math, os, sys
import pandas as pd
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, _REPO)

COST = 0.0025


def load():
    df = pd.read_parquet(os.path.join(_REPO, "state", "etf_daily.parquet"))
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="symbol", values="close").sort_index()


def metr(eq):
    eq = eq / eq.iloc[0]
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    dd = float((eq / eq.cummax() - 1).min())
    r = eq.pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    return eq.iloc[-1] - 1, cagr, dd, shp


def rotation(cl, assets, start):
    """주간 로테이션: 200일선 위 & 13주(65일) 모멘텀 최상위 1개 전액, 없으면 현금."""
    cl = cl[assets].dropna(how="all")
    ma = cl.rolling(200, min_periods=150).mean()
    mom = cl / cl.shift(65) - 1
    cl = cl[cl.index >= start]
    wk = cl.resample("W-FRI").last().index
    eq, cur, hold = 1.0, None, []
    out = []
    prev_px = {}
    for i, dt in enumerate(cl.index):
        if cur is not None and not pd.isna(cl.loc[dt, cur]):
            if cur in prev_px and prev_px[cur] > 0:
                eq *= cl.loc[dt, cur] / prev_px[cur]
            prev_px[cur] = cl.loc[dt, cur]
        if dt in wk or i == 0:                      # 주간 리밸런스
            elig = [a for a in assets if not pd.isna(cl.loc[dt, a]) and not pd.isna(ma.loc[dt, a])
                    and cl.loc[dt, a] > ma.loc[dt, a] and not pd.isna(mom.loc[dt, a])]
            tgt = max(elig, key=lambda a: mom.loc[dt, a]) if elig else None
            if tgt != cur:
                eq *= (1 - COST)                     # 전환 비용(왕복 근사)
                cur = tgt
                prev_px = {cur: cl.loc[dt, cur]} if cur else {}
                hold.append((str(dt.date()), cur or "CASH"))
        out.append((dt, eq))
    ser = pd.Series([e for _, e in out], index=[d for d, _ in out])
    return ser, hold


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    a = ap.parse_args()
    cl = load()
    names = {"069500": "KODEX200", "229200": "KOSDAQ150", "133690": "NASDAQ100", "132030": "GOLD", "360750": "SP500"}
    start = pd.Timestamp(a.start)
    combos = [
        ("KR-only (KODEX200/코스닥150)", ["069500", "229200"]),
        ("글로벌 (KR2+나스닥100)", ["069500", "229200", "133690"]),
        ("글로벌+금 (KR2+나스닥+골드)", ["069500", "229200", "133690", "132030"]),
        ("미국만 (나스닥100)", ["133690"]),
    ]
    print(f"\n=== 글로벌 ETF 로테이션 (주간·200일선 추세+13주 모멘텀 top1·비용 {COST:.2%}·KRW 환율내장) ===")
    print(f"  {'구성':34s} {'총':>9s} {'CAGR':>7s} {'MDD':>7s} {'Sharpe':>7s}")
    hold_g = None
    for lbl, assets in combos:
        ser, hold = rotation(cl, assets, start)
        t, c, d, s = metr(ser)
        print(f"  {lbl:34s} {t:+9.0%} {c:+7.1%} {d:+7.0%} {s:7.2f}")
        if "글로벌+금" in lbl:
            hold_g = hold
    for sym in ("069500", "133690", "132030"):
        ser = cl[sym].dropna(); ser = ser[ser.index >= start]
        t, c, d, s = metr(ser)
        print(f"  {'(보유) ' + names[sym]:34s} {t:+9.0%} {c:+7.1%} {d:+7.0%} {s:7.2f}")
    # 2026 YTD
    print("\n=== 2026 YTD ===")
    for lbl, assets in combos:
        ser, _ = rotation(cl, assets, pd.Timestamp("2026-01-02"))
        t, c, d, s = metr(ser)
        print(f"  {lbl:34s} {t:+9.1%}  MDD {d:+.0%}")
    if hold_g:
        print("\n글로벌+금 로테이션 최근 전환 8건:", hold_g[-8:])


if __name__ == "__main__":
    main()
