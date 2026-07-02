"""토스 캔들 페이지네이션으로 KRX ETF 장기 일봉 수집 → state/etf_daily.parquet.
자산(전부 KRW·환율 내장): 069500 KODEX200 · 229200 코스닥150 · 133690 TIGER나스닥100 · 132030 골드선물 · 360750 TIGER S&P500.
사용: .venv/bin/python scripts/_toss_fetch_etf.py
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
for ln in open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), encoding="utf-8"):
    ln = ln.strip()
    if "=" in ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from app.data import toss_api as T

K, S = os.environ["TOSS_API_KEY"], os.environ["TOSS_SECRET_KEY"]
SYMS = {"069500": "KODEX200", "229200": "KOSDAQ150", "133690": "NASDAQ100", "132030": "GOLD", "360750": "SP500"}
rows = []
for sym, name in SYMS.items():
    before, got = None, 0
    for page in range(40):                      # 최대 40페이지(8000일≈30년)
        try:
            c = T.get_candles(K, S, sym, interval="1d", count=200, before=before)
        except T.TossError as e:
            print(f"  {sym} p{page} FAIL {e.status}"); break
        cs = (c.get("result") or {}).get("candles") or []
        if not cs:
            break
        for x in cs:
            rows.append({"symbol": sym, "name": name, "date": x["timestamp"][:10], "close": float(x["closePrice"])})
        got += len(cs)
        before = cs[-1]["timestamp"]
        if len(cs) < 200:
            break
        time.sleep(0.12)                         # MARKET_DATA 10/s 여유
    print(f"{sym} {name}: {got}개")
df = pd.DataFrame(rows).drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "etf_daily.parquet")
df.to_parquet(out)
print("저장:", out, len(df), "행")
for sym, g in df.groupby("symbol"):
    print(f"  {sym} {SYMS[sym]}: {g['date'].min()} ~ {g['date'].max()} ({len(g)}일)")
