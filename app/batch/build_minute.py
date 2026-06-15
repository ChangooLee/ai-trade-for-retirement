"""KIS 1분봉 백필 → data/cache/minute_ohlcv.parquet (증분 적재).
KIS는 1분봉을 약 1년만 보관하므로 매일 cron으로 증분 적재해 영구 축적한다.
유니버스: 미지정 시 최신일 거래대금 상위 N + 보유 종목. 날짜: 최근 N 거래일(daily_ohlcv 기준).
사용: python -m app.batch.build_minute [--days 15] [--top 8] [--tickers 005930,000660]
"""
from __future__ import annotations
import argparse, os, sys, time
import pandas as pd, yaml
sys.path.insert(0, ".")
from app.data import kis_api as K  # noqa: E402
from app.data import krx_loader as L  # noqa: E402

OUT = "data/cache/minute_ohlcv.parquet"
HOLD = ["319400", "001820", "183300", "420770", "000990"]   # 현 보유(데모 포함)
COLS = ["ticker", "date", "time", "open", "high", "low", "close", "vol"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15); ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--tickers", default=None)
    a = ap.parse_args()
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
    dates = sorted(daily["date"].dt.strftime("%Y%m%d").unique())[-a.days:]
    if a.tickers:
        uni = a.tickers.split(",")
    else:
        snap = daily[daily["date"] == daily["date"].max()]
        uni = list(snap.sort_values("trdval", ascending=False)["ticker"].head(a.top))
    for h in HOLD:
        if h not in uni:
            uni.append(h)
    existing = pd.read_parquet(OUT) if os.path.exists(OUT) else pd.DataFrame(columns=COLS)
    have = set(zip(existing["ticker"], existing["date"])) if len(existing) else set()
    rows = []; done = 0; fail = 0
    print(f"백필: {len(uni)}종목 × {len(dates)}일 ({dates[0]}~{dates[-1]})", file=sys.stderr)
    for tk in uni:
        got = 0
        for d in dates:
            if (tk, d) in have:
                continue
            try:
                bars = K.get_minute_bars(tk, d)
                rows += [{"ticker": tk, **b} for b in bars]; got += len(bars); done += 1
            except Exception as e:
                fail += 1; print(f"  {tk} {d} 실패: {str(e)[:80]}", file=sys.stderr)
            time.sleep(0.05)
        print(f"  {tk}: +{got}행", file=sys.stderr)
    if rows:
        alld = pd.concat([existing, pd.DataFrame(rows)], ignore_index=True).drop_duplicates(["ticker", "date", "time"])
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        alld.to_parquet(OUT)
        print(f"적재 완료: +{len(rows)}행({done}종목일, 실패{fail}) → 총 {len(alld):,}행 · "
              f"{alld['ticker'].nunique()}종목 · {alld['date'].nunique()}일")
    else:
        print("신규 적재 없음(이미 보유)")


if __name__ == "__main__":
    main()
