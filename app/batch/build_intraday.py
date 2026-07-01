"""당일 종가 적재 — 장마감 후 pykrx로 '오늘' 종가를 기존 유니버스에 append (시뮬·백테스트 same-day용).
★공식 KRX OpenAPI는 당일 데이터를 익일 08시에야 공개 → 유니버스 재선정은 불가. 하지만 pykrx는 장후 당일 종가 제공.★
시뮬/백테스트는 유니버스 재선정이 아니라 '종가'만 필요하므로, 기존 daily_ohlcv 유니버스에 오늘 1행만 붙여 same-day 운용.
추천(주봉 20주선 눌림)은 완성된 주봉 기반이라 장중 안 바뀜 → 그대로(아침 빌드) 유지.
사용: python -m app.batch.build_intraday [--asof YYYYMMDD]
"""
from __future__ import annotations
import argparse, os, sys
import datetime as dt
import pandas as pd
sys.path.insert(0, ".")
from app.data import krx_loader as L  # noqa: E402

DAILY_COLS = ["date", "ticker", "name", "market", "open", "high", "low", "close", "volume", "trdval"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None, help="대상일 YYYYMMDD(기본=오늘 KST)")
    a = ap.parse_args()
    import yaml
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    dpath = cfg["paths"]["daily_ohlcv"]
    asof = a.asof or dt.datetime.now().strftime("%Y%m%d")
    asof_dt = pd.Timestamp(asof)
    if asof_dt.weekday() >= 5:
        print(f"{asof}는 주말 — 당일 종가 없음, 생략.", file=sys.stderr); return
    if not os.path.exists(dpath):
        print("daily_ohlcv 없음 — 아침 배치(update_data) 먼저 필요.", file=sys.stderr); return
    existing = L.load_daily_ohlcv(dpath)
    universe = set(existing["ticker"].astype(str))
    if existing["date"].max() >= asof_dt:
        print(f"이미 {asof} 이상 적재됨(최신 {str(existing['date'].max())[:10]}) — 생략.", file=sys.stderr); return

    from pykrx import stock
    rows = []
    for mk in ("KOSPI", "KOSDAQ"):
        try:
            d = stock.get_market_ohlcv_by_ticker(asof, market=mk)
        except Exception as e:
            print(f"  pykrx {mk} {asof} 실패: {str(e)[:80]}", file=sys.stderr); continue
        if d is None or not len(d):
            continue
        nm = {c: existing[existing["ticker"] == c]["name"].iloc[-1] for c in d.index if c in universe}
        for code, r in d.iterrows():
            code = str(code).zfill(6)
            if code not in universe or float(r.get("종가", 0)) <= 0:
                continue
            vol = float(r.get("거래량", 0)); cl = float(r["종가"])
            val = float(r.get("거래대금", 0)) or cl * vol
            rows.append({"date": asof_dt, "ticker": code, "name": nm.get(code, code), "market": mk,
                         "open": float(r.get("시가", cl)), "high": float(r.get("고가", cl)),
                         "low": float(r.get("저가", cl)), "close": cl, "volume": vol, "trdval": val})
    if not rows:
        print(f"{asof} 당일 종가 미수집(장중/휴장?) — 생략.", file=sys.stderr); return
    add = pd.DataFrame(rows)[DAILY_COLS]
    out = pd.concat([existing, add], ignore_index=True).drop_duplicates(["ticker", "date"], keep="last")
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    out.to_parquet(dpath)
    print(f"당일 종가 적재: {asof} +{len(add)}종목 → daily_ohlcv 최신 {str(out['date'].max())[:10]} ({len(out):,}행). "
          f"이제 build_webapp --asof {asof} + run_sims + bt_archive가 same-day로 동작.")


if __name__ == "__main__":
    main()
