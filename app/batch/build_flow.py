"""KIS EOD 종목별 투자자 순매수(외국인/기관/개인) 백필 → data/cache/investor_flow.parquet (증분).
1콜=30일 트레일링(FHKST01010900). 매일 cron 증분으로 영구 누적(분봉처럼 보관제한 없으나 30일 창이라 적재 필수).
유니버스: 미지정 시 최신 daily_ohlcv(시총top400) 전체. 사용: python -m app.batch.build_flow [--top 400]
"""
from __future__ import annotations
import argparse, os, sys, time
import pandas as pd, yaml
sys.path.insert(0, ".")
from app.data import kis_api as K  # noqa: E402
from app.data import krx_loader as L  # noqa: E402

OUT = "data/cache/investor_flow.parquet"
COLS = ["ticker", "date", "close", "frgn_ntby", "orgn_ntby", "prsn_ntby"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=400); ap.add_argument("--tickers", default=None)
    a = ap.parse_args()
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
    snap = daily[daily["date"] == daily["date"].max()]
    uni = a.tickers.split(",") if a.tickers else list(snap.sort_values("trdval", ascending=False)["ticker"].head(a.top))
    existing = pd.read_parquet(OUT) if os.path.exists(OUT) else pd.DataFrame(columns=COLS)
    acc = [existing] if len(existing) else []
    done = fail = total = 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    print(f"수급 백필: {len(uni)}종목 (1콜=30일 트레일링)", file=sys.stderr)
    for ti, tk in enumerate(uni):
        try:
            rows = K.get_investor_flow(tk)
            if rows:
                acc.append(pd.DataFrame([{"ticker": tk, **r} for r in rows])); total += len(rows); done += 1
        except Exception as e:
            fail += 1; print(f"  {tk} 실패: {str(e)[:80]}", file=sys.stderr)
        time.sleep(0.05)
        if (ti + 1) % 100 == 0 and acc:                  # 100종목마다 체크포인트
            pd.concat(acc, ignore_index=True).drop_duplicates(["ticker", "date"]).to_parquet(OUT)
            print(f"  ...{ti+1}/{len(uni)}", file=sys.stderr)
    if acc:
        alld = pd.concat(acc, ignore_index=True).drop_duplicates(["ticker", "date"])
        alld.to_parquet(OUT)
        print(f"적재 완료: +{total}행({done}종목·실패{fail}) → 총 {len(alld):,}행 · "
              f"{alld['ticker'].nunique()}종목 · {alld['date'].nunique()}일 ({alld['date'].min()}~{alld['date'].max()})")
    else:
        print("적재 없음")


if __name__ == "__main__":
    main()
