"""가치/퀄리티 팩터 패널 — pykrx 재무지표(PER·PBR·EPS·BPS·DIV) 백필. 생존편향 없는 팩터 백테스트용.
가치: 1/PER(이익수익률 E/P)·1/PBR(순자산수익률 B/P). 퀄리티: ROE ≈ EPS/BPS.
KRX 재무지표는 그날 거래된 전종목 반환(상폐 포함) → 과거 백필 시 생존편향 없음. 재시작 안전(수집된 날 스킵).
산출: data/cache/fundamental_v1.parquet (date,ticker,per,pbr,eps,bps,div)
사용: python -m app.batch.build_fundamental [--from 20170101] [--asof YYYYMMDD]
"""
from __future__ import annotations
import argparse, datetime as dt, os, sys, time
import pandas as pd
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _REPO)

PATH = os.path.join(_REPO, "data/cache/fundamental_v1.parquet")
COLS = ["date", "ticker", "per", "pbr", "eps", "bps", "div"]


def _day(bd):
    from pykrx import stock
    rows = []
    for mk in ("KOSPI", "KOSDAQ"):
        try:
            df = stock.get_market_fundamental_by_ticker(bd, market=mk)
        except Exception:
            df = None
        if df is None or not len(df):
            continue
        for code, r in df.iterrows():
            code = str(code).zfill(6)
            if not (code.isdigit() and len(code) == 6):
                continue
            rows.append((pd.Timestamp(bd), code, float(r.get("PER", 0) or 0), float(r.get("PBR", 0) or 0),
                         float(r.get("EPS", 0) or 0), float(r.get("BPS", 0) or 0), float(r.get("DIV", 0) or 0)))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="20170101")
    ap.add_argument("--asof", default=None)
    a = ap.parse_args()
    asof = a.asof or dt.datetime.now().strftime("%Y%m%d")
    existing = pd.read_parquet(PATH) if os.path.exists(PATH) else pd.DataFrame(columns=COLS)
    if len(existing):
        existing["date"] = pd.to_datetime(existing["date"])
    have = set(existing["date"].dt.strftime("%Y%m%d")) if len(existing) else set()
    days = pd.date_range(a.frm, asof, freq="D")
    todo = [d.strftime("%Y%m%d") for d in days if d.weekday() < 5 and d.strftime("%Y%m%d") not in have]
    print(f"재무지표 백필: {len(todo)}일 (보유 {len(have)}일) {a.frm}~{asof}", file=sys.stderr)
    frames = [existing] if len(existing) else []
    t0 = time.time(); got = 0
    for i, bd in enumerate(todo):
        rows = _day(bd)
        if rows:
            frames.append(pd.DataFrame(rows, columns=COLS)); got += 1
        if (i + 1) % 200 == 0:
            out = pd.concat(frames, ignore_index=True); out.to_parquet(PATH, index=False); frames = [out]
            el = time.time() - t0
            print(f"  {i+1}/{len(todo)} ({bd}) 수집 {got} · {el:.0f}s · 잔여 ~{el/(i+1)*(len(todo)-i-1)/60:.0f}분", file=sys.stderr)
    out = pd.concat(frames, ignore_index=True).drop_duplicates(["ticker", "date"], keep="last")
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    out.to_parquet(PATH, index=False)
    print(f"완료: {out['ticker'].nunique()}종목 · {out['date'].nunique()}일 · {len(out)}행 → {PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
