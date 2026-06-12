"""전략 복합 PIT 백테스트 — 장기 모멘텀 + 단타 오버나이트를 한 자본으로 운용.

목적: 두 전략의 상관·합산 위험을 측정. 둘 다 PIT(생존편향 제거) 패널로 일별 시뮬.
  · 장기 슬리브: 시총 상위 유니버스(거래대금 20억+) F리더∩20주선 눌림, 40거래일 시간청산,
                D4 노출. 자본의 ALLOC_LONG 비중.
  · 단타 슬리브: 거래량3배+양봉(+3~28.5%) → 종가 매수, 익일 시가 매도(1박). Risk-Off 국면만.
                자본의 ALLOC_SHORT 비중, 1박이라 매일 회전.
배분: 일 단위로 각 슬리브 평가액을 합산해 총자산 곡선 구성. 상관·기여·합산 MDD 보고.
정직: 단타 슬리브도 PIT 패널(상폐 포함)에서 산출 — 단 분 단위 슬리피지 미반영(상한 해석).

사용: python -m backtest.composite_strategy_backtest [--alloc-long 0.7] [--top 400]
"""
from __future__ import annotations
import argparse, math, sys, time
import numpy as np, pandas as pd, yaml

sys.path.insert(0, ".")
from app.data import krx_loader as L  # noqa: E402
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock  # noqa: E402
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402

COST = 0.0035


def metrics(eqs, cap0):
    eqs = eqs.dropna()
    yrs = (eqs.index[-1] - eqs.index[0]).days / 365.25
    cagr = (eqs.iloc[-1] / cap0) ** (1 / yrs) - 1
    dd = (eqs / eqs.cummax() - 1).min()
    r = eqs.pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    return cagr, dd, shp, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alloc-long", type=float, default=0.7)
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--liq", type=float, default=2e9)
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    H = cfg["holding"]["max_holding_days"]; cap0 = cfg["portfolio"]["initial_capital"]
    maxpos = cfg["sizing"]["max_positions"]; baseslot = cfg["sizing"]["base_slot_weight"]
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]

    daily = load_adjusted()
    index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    t0 = time.time()
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]; n = len(cal)
    opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal).where(lambda x: x > 0)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal).where(lambda x: x > 0)
    vol = daily.pivot_table(index="date", columns="ticker", values="volume").reindex(cal)
    tv = daily.pivot_table(index="date", columns="ticker", values="trdval").reindex(cal)
    by_date = {d: g for d, g in di.groupby("date")}
    print(f"공통 지표 {time.time()-t0:.0f}s · 거래일 {n}", file=sys.stderr)

    def px(mat, i, tk):
        v = mat.iloc[i].get(tk)
        return float(v) if v is not None and np.isfinite(v) and v > 0 else None

    # D4 국면 캐시 (일별)
    mode = {}
    for i in range(n):
        t = cal[i]
        wkey = t.to_period("W")
        if wkey not in mode:
            try:
                mode[wkey] = compute_d4_exposure(index[index["date"] <= t], t, cfg)
            except Exception:
                mode[wkey] = {"target_exposure": 0.4, "mode": "Risk-Off"}
    dmode = [mode[cal[i].to_period("W")] for i in range(n)]

    # 단타 신호 패널 (전부 벡터화)
    ret1 = cls.pct_change()
    vol20 = vol.rolling(20).mean()
    liq_on = tv.rolling(20).mean() > 1e9
    on_sig = (vol > 3 * vol20) & (ret1 > 0.03) & (ret1 < 0.285) & liq_on
    on_ret = (opn.shift(-1) / cls - 1)            # 종가 매수 → 익일 시가

    frm = pd.Timestamp("20170501")
    start_i = next(i for i in range(n) if cal[i] >= frm)

    # ---------- 장기 슬리브 (주간 리밸런스, 자본 = alloc_long × 총자산 비례 노출) ----------
    # 단순화: 장기 슬리브의 '수익률 시계열'을 독립 계산(자기 자본 100% 기준), 단타도 동일.
    #         최종 합성은 두 수익률을 alloc 비중으로 결합(매일 리밸런싱 가정).
    def long_daily_returns():
        cap = float(cap0); pos = {}; rets = pd.Series(0.0, index=cal[start_i:])
        rebal = set(range(start_i, n - 1, 5))
        wcache = {}
        prev_val = cap
        for i in range(start_i, n):
            t = cal[i]
            # 일별 평가: 보유 시가평가 변화 반영 위해 종가 기준 자산
            val = cap + sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
            if i > start_i:
                rets.iloc[i - start_i] = val / prev_val - 1
            prev_val = val
            # 청산
            for tk, p in list(pos.items()):
                if (i - p["eidx"]) >= H:
                    sp = None
                    for j in range(i + 1, min(i + 41, n)):
                        sp = px(opn, j, tk)
                        if sp:
                            break
                    cap += p["sh"] * sp * (1 - COST / 2) if sp else 0
                    del pos[tk]
            if i not in rebal or i + 1 >= n:
                continue
            snap = by_date.get(t)
            if snap is None:
                continue
            u = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml) & (snap.get("avg_trdval20", 0) > args.liq)]
            u = u.sort_values("mktcap", ascending=False).head(args.top)
            if len(u) < 50:
                continue
            ex = dmode[i]; slots = compute_target_slots(ex["target_exposure"], maxpos, baseslot)
            weight = compute_weight_per_stock(ex["target_exposure"], slots)
            if slots <= len(pos):
                continue
            lead = compute_leader_flags(u, cfg)
            wc = last_completed_week_cutoff(t)
            if wc not in wcache:
                wcache[wc] = wk[wk["week_end"] <= wc].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
            pull = compute_pullback_flags(wcache[wc], cfg)
            mg = lead.merge(pull[["ticker", "pullback_20w_105"]], on="ticker", how="left")
            cands = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)].sort_values("rs_rank", ascending=False)
            eq_now = cap + sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
            for tk in cands["ticker"]:
                if len(pos) >= slots:
                    break
                if tk in pos:
                    continue
                bp = px(opn, i + 1, tk)
                if bp is None:
                    continue
                sh = math.floor(weight * eq_now / bp)
                if sh > 0 and cap >= sh * bp * (1 + COST / 2):
                    cap -= sh * bp * (1 + COST / 2); pos[tk] = {"eidx": i + 1, "epx": bp, "sh": sh}
        return rets

    # ---------- 단타 슬리브 (일별 수익률; Risk-Off 국면만, K=8 거래대금순) ----------
    def overnight_daily_returns():
        rets = pd.Series(0.0, index=cal[start_i:])
        for i in range(start_i, n - 1):
            if dmode[i]["mode"] != "Risk-Off":
                continue
            row_sig = on_sig.iloc[i]
            picks = tv.iloc[i].where(row_sig).nlargest(8).index
            if len(picks) == 0:
                continue
            r = on_ret.iloc[i][picks].dropna()
            if len(r):
                rets.iloc[i - start_i] = r.mean() - COST     # 동일가중, 신호일 종가→익일 시가
        return rets

    rl = long_daily_returns()
    print(f"장기 슬리브 {time.time()-t0:.0f}s", file=sys.stderr)
    rs = overnight_daily_returns()
    print(f"단타 슬리브 {time.time()-t0:.0f}s", file=sys.stderr)

    # 상관
    both = pd.concat([rl, rs], axis=1).dropna()
    corr = both.iloc[:, 0].corr(both.iloc[:, 1])
    on_days = int((rs != 0).sum())

    # 합성 (매일 alloc 비중 리밸런싱)
    aL = args.alloc_long
    print(f"\n=== 전략 복합 PIT (생존편향 제거, {cal[start_i].date()}~{cal[-1].date()}) ===")
    print(f"  단타 거래일 {on_days}일(Risk-Off 한정) · 장기·단타 일수익 상관 {corr:+.3f}")
    print(f"  {'구성':<28s}{'CAGR':>8s}{'MDD':>8s}{'Sharpe':>8s}")
    configs = [("장기 100%", 1.0, 0.0), ("단타 100%", 0.0, 1.0),
               (f"복합 장기{aL:.0%}+단타{1-aL:.0%}", aL, 1 - aL),
               ("복합 80/20", 0.8, 0.2), ("복합 60/40", 0.6, 0.4), ("복합 50/50", 0.5, 0.5)]
    for name, wl, ws in configs:
        comb = (wl * rl.fillna(0) + ws * rs.fillna(0))
        eq = cap0 * (1 + comb).cumprod()
        cagr, dd, shp, _ = metrics(eq, cap0)
        print(f"  {name:<28s}{cagr*100:+7.1f}%{dd*100:+7.1f}%{shp:8.2f}")
    print("  ※ 상관이 낮을수록 복합의 MDD 완화 효과 큼. 단타는 분단위 슬리피지 미반영(상한).")


if __name__ == "__main__":
    main()
