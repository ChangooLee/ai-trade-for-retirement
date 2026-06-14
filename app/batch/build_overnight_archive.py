"""오버나이트 단타 백테스트 아카이브 — 화면 '단타 백테스트' 기능용.

매 거래일의 신호종목(거래량>20일평균×3 & 등락 +3~28.5% & 20일평균 거래대금 10억+, 거래대금 상위 8)과
각 종목의 1박 수익(익일시가/당일종가−1), 그날 D4 국면을 state/overnight_days.json에 저장.
증분: 이미 계산된 날은 건너뜀. 가격: 가능하면 PIT 조정가(생존편향 제거)·없으면 라이브 daily_ohlcv(서버).

사용: python -m app.batch.build_overnight_archive [--from 2017-01-01]
"""
from __future__ import annotations
import argparse, json, os, sys, time
import numpy as np, pandas as pd, yaml

sys.path.insert(0, ".")
from app.data.calendar import trading_calendar  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402
from app.data import krx_loader as L  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(_REPO, "state", "overnight_days.json")
K = 8                       # 화면 신호 표시 개수와 동일


def main():
    # 화면 신호·검증 스크립트(overnight_deployed_backtest)와 동일하게 라이브 daily_ohlcv(상위 유동성) 사용.
    # PIT 전종목은 동전주 잡신호가 섞여 결과를 왜곡 → daily_ohlcv가 화면과 일치. 서버에도 있어 일배치 빌드 가능.
    ap = argparse.ArgumentParser(); ap.add_argument("--from", dest="frm", default="2017-01-01")
    a = ap.parse_args()
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"]); index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    t0 = time.time()
    ds = daily.sort_values(["ticker", "date"]).copy()
    g = ds.groupby("ticker", group_keys=False)
    ds["ret1"] = g["close"].pct_change(fill_method=None)
    ds["vol20"] = g["volume"].transform(lambda s: s.rolling(20).mean())
    if "trdval" not in ds.columns:
        ds["trdval"] = ds["close"] * ds["volume"]
    ds["tv20"] = g["trdval"].transform(lambda s: s.rolling(20).mean())
    ds["next_open"] = g["open"].shift(-1)
    name_of = dict(zip(daily["ticker"], daily.get("name", daily["ticker"])))
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]

    arch = {"days": {}}
    if os.path.exists(OUT):
        try: arch = json.load(open(OUT, encoding="utf-8"))
        except Exception: pass
    done = set(arch.get("days", {}).keys())
    frm = pd.Timestamp(a.frm)
    sig = ds[(ds["close"] > 0) & (ds["next_open"] > 0) & (ds["tv20"] > 1e9) &
             (ds["volume"] > 3 * ds["vol20"]) & (ds["ret1"] > 0.03) & (ds["ret1"] < 0.285)].copy()
    sig["r_on"] = sig["next_open"] / sig["close"] - 1
    by_date = {d: x for d, x in sig.groupby("date")}
    mcache = {}; new = 0
    for d in cal:
        if d < frm:
            continue
        ds_ = str(d.date())
        if ds_ in done:
            continue
        sub = by_date.get(d)
        if sub is None or len(sub) == 0:
            arch["days"][ds_] = {"sigs": [], "m": None}; continue
        wk = d.to_period("W")
        if wk not in mcache:
            try: mcache[wk] = compute_d4_exposure(index[index["date"] <= d], d, cfg)["mode"]
            except Exception: mcache[wk] = "?"
        top = sub.sort_values("trdval", ascending=False).head(K)
        sigs = [{"t": r["ticker"], "n": name_of.get(r["ticker"], r["ticker"]), "r": round(float(r["r_on"]), 5)}
                for _, r in top.iterrows()]
        arch["days"][ds_] = {"sigs": sigs, "m": mcache[wk]}
        new += 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    json.dump(arch, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"오버나이트 아카이브: +{new}일 · 총 {len(arch['days'])}일 · {os.path.getsize(OUT)//1024}KB · {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
