"""풀 엔진 PIT 생존편향제거 백테스트 — 시간청산(40일) + 20주선 추세이탈 청산 + 월 −3% 서킷브레이커.
'우리 전략이 생존편향 제거하고도 돈을 버는가'의 결정적 답.
단순(시간청산만) vs 풀(+추세이탈+CB) × baseline vs 52주고점≥0.90. pit_portfolio_backtest 로직 + 엔진 청산/CB.
사용: python -m backtest.full_pit_backtest
"""
from __future__ import annotations
import math, os, sys
import numpy as np, pandas as pd, yaml
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock  # noqa: E402
from backtest.early_cut_diagnostic import load_adjusted  # noqa: E402


def main():
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    H = cfg["holding"]["max_holding_days"]; cost = cfg["cost"]["assumed_round_trip_cost"]
    cap0 = cfg["portfolio"]["initial_capital"]; maxpos = cfg["sizing"]["max_positions"]; baseslot = cfg["sizing"]["base_slot_weight"]
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]; top = 300; step = 5
    daily = load_adjusted(); index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]; n = len(cal)
    opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal); opn = opn.where(opn > 0)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal); cls = cls.where(cls > 0)
    by_date = {d: g for d, g in di.groupby("date")}
    ecache = {}; wcache = {}

    def px(mat, i, tk):
        v = mat.iloc[i].get(tk)
        return float(v) if v is not None and np.isfinite(v) and v > 0 else None

    def sell_px(i, tk):
        for j in range(i, min(i + 41, n)):
            p = px(opn, j, tk)
            if p is not None:
                return p
        return 0.0

    def run(min_hi52, trend_exit, cb_limit):
        cash = float(cap0); pos = {}; eqc = []; trades = []; wo = 0; cb_month = None; cb_base = 0.0; trips = 0
        for i in range(0, n - step - 1, step):
            t = cal[i]
            eqc.append((t, cash + sum(p["sh"] * (px(cls, i, tk) or px(cls, max(i - 5, 0), tk) or 0) for tk, p in pos.items())))
            snap = by_date.get(t)
            wc = last_completed_week_cutoff(t)
            if wc not in wcache:
                wcache[wc] = wk[wk["week_end"] <= wc].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
            wma_map = dict(zip(wcache[wc]["ticker"], wcache[wc]["w_ma20"]))
            mom_map = dict(zip(snap["ticker"], snap["mom_6m_1m"])) if snap is not None else {}
            # 청산: 시간 H OR 추세이탈(종가<20주선 & 6-1M 모멘텀<0)
            for tk, p in list(pos.items()):
                te = False
                if trend_exit:
                    cpx = px(cls, i, tk); wm = wma_map.get(tk); mo = mom_map.get(tk)
                    te = bool(cpx and wm and cpx < wm and (mo is not None and mo < 0))
                if (i - p["eidx"]) >= H or te:
                    sp = sell_px(i + 1, tk)
                    if sp <= 0:
                        wo += 1; trades.append(-1.0); del pos[tk]; continue
                    cash += p["sh"] * sp * (1 - cost / 2); trades.append(sp / p["epx"] - 1); del pos[tk]
            # CB: 청산 후 월손익 ≤ −limit → 그달 신규매수 차단(block)
            eqp = cash + sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
            cur_pnl = eqp - cap0; mon = str(t.date())[:7]
            if mon != cb_month:
                cb_month = mon; cb_base = cur_pnl
            tripped = (cb_limit > 0) and ((cur_pnl - cb_base) <= -cb_limit * cap0)
            if tripped:
                trips += 1
            if snap is None or tripped:
                continue
            u = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml)].sort_values("avg_trdval20", ascending=False).head(top)
            if len(u) < 50:
                continue
            wkey = t.to_period("W")
            if wkey not in ecache:
                try:
                    ecache[wkey] = compute_d4_exposure(index[index["date"] <= t], t, cfg)["target_exposure"]
                except Exception:
                    ecache[wkey] = 0.4
            exp = ecache[wkey]
            slots = compute_target_slots(exp, maxpos, baseslot); weight = compute_weight_per_stock(exp, slots)
            if slots > len(pos):
                lead = compute_leader_flags(u, cfg); pull = compute_pullback_flags(wcache[wc], cfg)
                mg = lead.merge(pull[["ticker", "pullback_20w_105"]], on="ticker", how="left").merge(
                    u[["ticker", "high_52w_ratio"]].rename(columns={"high_52w_ratio": "_h"}), on="ticker", how="left")
                cands = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)
                          & (mg["_h"].fillna(0) >= min_hi52)].sort_values("rs_rank", ascending=False)
                eqn = cash + sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
                for tk in cands["ticker"]:
                    if len(pos) >= slots:
                        break
                    if tk in pos:
                        continue
                    bp = px(opn, i + 1, tk)
                    if bp is None:
                        continue
                    sh = math.floor(weight * eqn / bp); amt = sh * bp * (1 + cost / 2)
                    if sh > 0 and cash >= amt:
                        cash -= amt; pos[tk] = {"eidx": i + 1, "epx": bp, "sh": sh}
        fe = cash + sum(p["sh"] * (px(cls, n - 1, tk) or 0) for tk, p in pos.items())
        eq = pd.DataFrame(eqc, columns=["d", "eq"]).set_index("d")
        yrs = (eqc[-1][0] - eqc[0][0]).days / 365.25
        cagr = (fe / cap0) ** (1 / yrs) - 1 if fe > 0 else -1
        dd = float((eq["eq"] / eq["eq"].cummax() - 1).min()); r = eq["eq"].pct_change().dropna()
        shp = r.mean() / r.std() * math.sqrt(52) if r.std() > 0 else 0
        winr = float(np.mean([x > 0 for x in trades])) if trades else 0
        return dict(cagr=cagr, ret=fe / cap0 - 1, mdd=dd, shp=shp, n=len(trades), winr=winr, trips=trips)

    print(f"\n=== 풀 엔진 PIT 생존편향제거 ({cal[0].date()}~{cal[-1].date()}) · 노출×1 ===")
    for label, mh, te, cb in [("단순 baseline(시간청산만)", 0.0, False, 0.0),
                              ("단순 52주≥0.90", 0.90, False, 0.0),
                              ("풀 baseline(+추세이탈+CB−3%)", 0.0, True, 0.03),
                              ("풀 52주≥0.90(+추세이탈+CB−3%)", 0.90, True, 0.03)]:
        m = run(mh, te, cb)
        print(f"  {label:30s}: CAGR {m['cagr']:+.1%} | MDD {m['mdd']:+.1%} | Sharpe {m['shp']:.2f} | "
              f"거래 {m['n']} 승률 {m['winr']:.0%} CB발동 {m['trips']} | 총 {m['ret']:+.0%}")


if __name__ == "__main__":
    main()
