"""기간 백테스트용 아카이브 백필 — 과거 전 거래일의 일별 시그널·가격을 저장.

화면의 '기간 백테스트' 기능이 빠르게 엔진을 재생할 수 있도록, 매 거래일의
  buy(F리더∩20주선 눌림 후보) · sells(20주선 이탈) · m(D4 목표노출) → state/bt_days.json
  시총 top400 종가(date,ticker,close)                              → state/bt_prices.parquet
를 미리 계산해 둔다. 증분: 이미 계산된 날짜는 건너뛰고 새 거래일만 추가(일일 배치에서 호출).

사용: python -m app.batch.build_bt_archive [--from 2017-05-01]
"""
from __future__ import annotations
import argparse, json, os, sys, time
import numpy as np, pandas as pd, yaml

sys.path.insert(0, ".")
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.data import krx_loader as L  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_daily(cfg):
    """가격 패널: 가능하면 PIT 조정가(생존편향 제거, 로컬 백필) · 없으면 라이브 daily_ohlcv(서버 증분)."""
    try:
        from backtest.pit_mktcap_backtest import load_adjusted
        return load_adjusted()
    except Exception:
        return L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
DAYS_PATH = os.path.join(_REPO, "state", "bt_days.json")
PRICES_PATH = os.path.join(_REPO, "state", "bt_prices.parquet")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="2017-05-01")
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
    liq = cfg["universe"].get("min_trdval", 5e8); top_n = cfg["universe"]["top_n"]
    daily = _load_daily(cfg); index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    t0 = time.time()
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]
    by_date = {d: g for d, g in di.groupby("date")}
    name_of = dict(zip(daily["ticker"], daily["name"]))

    arch = {"names": {}, "calendar": [], "days": {}, "sizing": {
        "max_positions": int(cfg["sizing"]["max_positions"]), "base_slot_weight": float(cfg["sizing"]["base_slot_weight"]),
        "hold_days": int(cfg["holding"]["max_holding_days"]), "cost": float(cfg["cost"]["assumed_round_trip_cost"])}}
    if os.path.exists(DAYS_PATH):
        try: arch = json.load(open(DAYS_PATH, encoding="utf-8"))
        except Exception: pass
    done = set(arch.get("days", {}).keys())
    price_rows = []
    if os.path.exists(PRICES_PATH):
        try: price_rows.append(pd.read_parquet(PRICES_PATH))
        except Exception: pass

    frm = pd.Timestamp(args.frm)
    wcache, ecache = {}, {}; new = 0; newpx = []
    for d in cal:
        if d < frm:
            continue
        ds = str(d.date())
        if ds in done:
            continue
        snap = by_date.get(d)
        if snap is None:
            continue
        uni = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml) & (snap.get("avg_trdval20", 0) > liq)]
        uni = uni.sort_values("mktcap", ascending=False).head(top_n)
        if len(uni) < 50:
            continue
        wkey = d.to_period("W")
        if wkey not in ecache:
            try: ecache[wkey] = compute_d4_exposure(index[index["date"] <= d], d, cfg)["target_exposure"]
            except Exception: ecache[wkey] = 0.4
        lead = compute_leader_flags(uni, cfg); wc = last_completed_week_cutoff(d)
        if wc not in wcache:
            wcache[wc] = wk[wk["week_end"] <= wc].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
        pull = compute_pullback_flags(wcache[wc], cfg)
        mg = lead.merge(pull[["ticker", "pullback_20w_105", "w_ma20"]], on="ticker", how="left")
        cand = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)].sort_values("rs_rank", ascending=False)
        buy = [tk for tk in cand["ticker"]]
        mg["bw"] = mg["close"] / mg["w_ma20"] - 1
        sells = list(mg[(mg["bw"] < 0) & (mg["mom_6m_1m"] < 0)]["ticker"])
        arch["days"][ds] = {"buy": buy, "sells": sells, "m": float(ecache[wkey])}
        for _, r in uni.iterrows():
            if r["close"] > 0:
                newpx.append((ds, r["ticker"], float(r["close"])))
                if r["ticker"] not in arch["names"]:
                    arch["names"][r["ticker"]] = name_of.get(r["ticker"], r["ticker"])
        new += 1
        if new % 200 == 0:
            print(f"  {new}일 처리 · {ds} · {time.time()-t0:.0f}s", file=sys.stderr)

    arch["calendar"] = [str(d.date()) for d in cal]
    os.makedirs(os.path.dirname(DAYS_PATH), exist_ok=True)
    tmp = DAYS_PATH + ".tmp"
    json.dump(arch, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DAYS_PATH)
    if newpx:
        df = pd.DataFrame(newpx, columns=["date", "ticker", "close"])
        if price_rows:
            df = pd.concat(price_rows + [df], ignore_index=True).drop_duplicates(["date", "ticker"], keep="last")
        df.to_parquet(PRICES_PATH, index=False)
    print(f"아카이브: +{new}일 · 총 {len(arch['days'])}일 · 가격 {os.path.getsize(PRICES_PATH)//1024 if os.path.exists(PRICES_PATH) else 0}KB · {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
