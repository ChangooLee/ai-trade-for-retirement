"""시뮬레이터 엔진을 2026-01부터 일별 재생 — '이 로직'으로 ₩1,000만 시작 시 현재 수익.

라이브 시뮬레이터가 매일 하는 일을 그대로 과거에 적용: 매 거래일 그날 시그널(시총top400 F리더∩20주선
눌림 매수 · 40거래일/20주선 청산 · D4 노출 · 월 −3% 서킷브레이커, TDA 자문전용=청산 미사용)을 만들어
app.sim.engine.execute_day로 한 스텝씩 전진. 생존편향 제거 PIT 조정가(load_adjusted) 사용.

사용: python -m backtest.sim_replay_2026 [--start 2026-01-01] [--capital 10000000]
"""
from __future__ import annotations
import argparse, math, sys, time
import numpy as np, pandas as pd, yaml

sys.path.insert(0, ".")
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock  # noqa: E402
from app.data import krx_loader as L  # noqa: E402
from app.sim import engine  # noqa: E402
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--capital", type=float, default=10_000_000)
    ap.add_argument("--cb-limit", type=float, default=0.03, help="월 손실 한도(0=끔). 느슨할수록 공격적")
    ap.add_argument("--exposure-mult", type=float, default=1.0, help="D4 목표노출 배수(1.0=기본, 캡 1.0)")
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    H = cfg["holding"]["max_holding_days"]; costv = cfg["cost"]["assumed_round_trip_cost"]
    maxpos = cfg["sizing"]["max_positions"]; baseslot = cfg["sizing"]["base_slot_weight"]
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
    liq = cfg["universe"].get("min_trdval", 5e8); top_n = cfg["universe"]["top_n"]

    daily = load_adjusted()
    index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    t0 = time.time()
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    calall = [pd.Timestamp(d) for d in trading_calendar(daily)]
    by_date = {d: g for d, g in di.groupby("date")}
    name_of = dict(zip(daily["ticker"], daily["name"]))
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(calall).where(lambda x: x > 0)
    start = pd.Timestamp(args.start)
    days = [d for d in calall if d >= start]
    calstr = [str(d.date()) for d in calall]
    print(f"지표 {time.time()-t0:.0f}s · 재생 거래일 {len(days)} ({days[0].date()}~{days[-1].date()})", file=sys.stderr)

    wcache, ecache = {}, {}
    state = engine.new_state(args.capital, cb_limit=args.cb_limit)
    eq_curve = []; trips = 0
    for d in days:
        i = calall.index(d)
        snap = by_date.get(d)
        buy_order = []; sells = []; slots = 0; weight = 0.0; prices = {}
        if snap is not None:
            uni = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml) & (snap.get("avg_trdval20", 0) > liq)]
            uni = uni.sort_values("mktcap", ascending=False).head(top_n)
            if len(uni) >= 50:
                prices = {r["ticker"]: float(r["close"]) for _, r in uni.iterrows() if r["close"] > 0}
                wkey = d.to_period("W")
                if wkey not in ecache:
                    try: ecache[wkey] = compute_d4_exposure(index[index["date"] <= d], d, cfg)["target_exposure"]
                    except Exception: ecache[wkey] = 0.4
                m = min(1.0, ecache[wkey] * args.exposure_mult)     # 공격성: 목표노출 배수(캡 100%)
                slots = compute_target_slots(m, maxpos, baseslot); weight = compute_weight_per_stock(m, slots)
                lead = compute_leader_flags(uni, cfg)
                wc = last_completed_week_cutoff(d)
                if wc not in wcache:
                    wcache[wc] = wk[wk["week_end"] <= wc].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
                pull = compute_pullback_flags(wcache[wc], cfg)
                mg = lead.merge(pull[["ticker", "pullback_20w_105", "w_ma20"]], on="ticker", how="left")
                cand = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)].sort_values("rs_rank", ascending=False)
                buy_order = [{"ticker": tk, "name": name_of.get(tk, tk), "close": float(prices[tk])}
                             for tk in cand["ticker"] if tk in prices]
                # 20주선 이탈 청산 후보 (close<w_ma20 & 6-1M 모멘텀<0)
                leg = mg.copy(); leg["below_w"] = leg["close"] / leg["w_ma20"] - 1
                sells = list(leg[(leg["below_w"] < 0) & (leg["mom_6m_1m"] < 0)]["ticker"])
        sig = {"hold_days": H, "cost": costv, "exposure": {"slots": int(slots), "weight": weight},
               "buy_order": buy_order, "sell_tickers": sells, "prices": prices, "calendar": calstr}
        state, res = engine.execute_day(state, str(d.date()), sig)
        if res["tripped"]: trips += 1
        eq_curve.append((d, res["equity"]))

    eqs = pd.Series([e for _, e in eq_curve], index=[d for d, _ in eq_curve])
    final = eqs.iloc[-1]; inv = args.capital
    mdd = (eqs / eqs.cummax() - 1).min()
    print(f"\n=== 시뮬레이터 재생 백테스트 (PIT 조정가, 시총top{top_n}, {days[0].date()}~{days[-1].date()}) ===")
    print(f"  투자금(원금)   ₩{inv:,.0f}")
    print(f"  현재 평가자산  ₩{final:,.0f}")
    print(f"  총손익         ₩{final-inv:,.0f}  ({final/inv-1:+.1%})")
    print(f"  기간 MDD       {mdd:+.1%}  ·  서킷브레이커 발동(당월한도 도달) 일수 {trips}")
    print(f"  보유 종목수    {len(state['positions'])} · 현금 ₩{state['cash']:,.0f}")
    # 월별 자산
    mo = eqs.resample("ME").last()
    print("  월말 평가자산:")
    for dt_, v in mo.items():
        print(f"    {dt_.strftime('%Y-%m')}: ₩{v:,.0f} ({v/inv-1:+.1%})")
    print(f"  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
