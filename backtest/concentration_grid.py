"""집중도 격자 PIT 백테스트 — 수익을 높이는 두 레버(유니버스 크기·포지션 집중)를 견고성과 함께 스윕.

사용자 요청(결정 0005 후속): 시총 유니버스 유지하되 집중도를 높여 수익 레버를 찾되,
**과최적(N 민감) 함정을 분할검증으로 거른다**. 각 조합마다:
  · 전체 총수익/CAGR/MDD/Sharpe
  · 전반(2017–21)/후반(2022–26) 분할 — 둘 다 +여야 신뢰
  · 최근4년(2023–26) 누적 — 현재 레짐 적합성
  · 인접 조합과 일관돼야 신뢰(단일 스파이크=과최적)

레버: top_n(유니버스 크기) · base_slot_weight(종목당 비중 — 클수록 적게·집중) · max_positions(상한).
사용: python -m backtest.concentration_grid
"""
from __future__ import annotations
import math, sys, time
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

# (라벨, top_n, base_slot_weight, max_positions)
COMBOS = [
    ("기준 top300·슬롯5%·15종목", 300, 0.05, 15),
    ("top350·슬롯5%·15종목",      350, 0.05, 15),
    ("top400·슬롯5%·15종목",      400, 0.05, 15),
    ("top450·슬롯5%·15종목",      450, 0.05, 15),
    ("top300·슬롯7%·12종목(집중)", 300, 0.07, 12),
    ("top300·슬롯10%·10종목(집중)",300, 0.10, 10),
    ("top400·슬롯7%·12종목(집중)", 400, 0.07, 12),
    ("top400·슬롯10%·10종목(집중)",400, 0.10, 10),
]


def main():
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    H = cfg["holding"]["max_holding_days"]; cost = cfg["cost"]["assumed_round_trip_cost"]
    cap0 = cfg["portfolio"]["initial_capital"]
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
    liq = cfg["universe"].get("min_trdval", 5e8)
    daily = load_adjusted()
    index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    t0 = time.time()
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]; n = len(cal)
    opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal).where(lambda x: x > 0)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal).where(lambda x: x > 0)
    by_date = {d: g for d, g in di.groupby("date")}
    wcache, ecache = {}, {}
    print(f"공통 지표 {time.time()-t0:.0f}s · 거래일 {n}", file=sys.stderr)

    def px(mat, i, tk):
        v = mat.iloc[i].get(tk)
        return float(v) if v is not None and np.isfinite(v) and v > 0 else None

    def sell_px(i, tk):
        for j in range(i, min(i + 41, n)):
            p = px(opn, j, tk)
            if p is not None:
                return p
        return 0.0

    frm = pd.Timestamp("20170501")
    rebal = [i for i in range(0, n - 6, 5) if cal[i] >= frm]

    def mktcap_sel(top_n):
        return lambda u: u[u["avg_trdval20"] > liq].sort_values("mktcap", ascending=False).head(top_n)

    def run(top_n, baseslot, maxpos):
        select = mktcap_sel(top_n)
        cash = float(cap0); pos = {}; eqc = []
        for i in rebal:
            t = cal[i]
            held = sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
            eqc.append((t, cash + held))
            for tk, p in list(pos.items()):
                if (i - p["eidx"]) >= H:
                    sp = sell_px(i + 1, tk)
                    cash += p["sh"] * sp * (1 - cost / 2) if sp > 0 else 0
                    del pos[tk]
            snap = by_date.get(t)
            if snap is None:
                continue
            u = select(snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml)])
            if len(u) < 50:
                continue
            if t not in ecache:
                ecache[t] = compute_d4_exposure(index[index["date"] <= t], t, cfg)["target_exposure"]
            m = ecache[t]
            slots = compute_target_slots(m, maxpos, baseslot); weight = compute_weight_per_stock(m, slots)
            if slots > len(pos):
                lead = compute_leader_flags(u, cfg)
                wc = last_completed_week_cutoff(t)
                if wc not in wcache:
                    wcache[wc] = wk[wk["week_end"] <= wc].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
                pull = compute_pullback_flags(wcache[wc], cfg)
                mg = lead.merge(pull[["ticker", "pullback_20w_105"]], on="ticker", how="left")
                cands = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)].sort_values("rs_rank", ascending=False)
                eq_now = cash + sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
                for tk in cands["ticker"]:
                    if len(pos) >= slots:
                        break
                    if tk in pos:
                        continue
                    bp = px(opn, i + 1, tk)
                    if bp is None:
                        continue
                    sh = math.floor(weight * eq_now / bp)
                    if sh > 0 and cash >= sh * bp * (1 + cost / 2):
                        cash -= sh * bp * (1 + cost / 2); pos[tk] = {"eidx": i + 1, "epx": bp, "sh": sh}
        final = cash + sum(p["sh"] * (px(cls, n - 1, tk) or 0) for tk, p in pos.items())
        eqs = pd.Series([e for _, e in eqc], index=[d for d, _ in eqc])
        return final, eqs

    def metrics(eqs, final):
        yrs = (eqs.index[-1] - eqs.index[0]).days / 365.25
        cagr = (final / cap0) ** (1 / yrs) - 1
        dd = (eqs / eqs.cummax() - 1).min()
        r = eqs.pct_change().dropna()
        shp = r.mean() / r.std() * math.sqrt(52) if r.std() > 0 else 0
        mid = pd.Timestamp("20220101")
        e1, e2 = eqs[eqs.index < mid], eqs[eqs.index >= mid]
        h1 = (e1.iloc[-1] / e1.iloc[0] - 1) if len(e1) > 5 else float("nan")
        h2 = (e2.iloc[-1] / e2.iloc[0] - 1) if len(e2) > 5 else float("nan")
        e3 = eqs[eqs.index >= pd.Timestamp("20230101")]
        recent = (e3.iloc[-1] / e3.iloc[0] - 1) if len(e3) > 5 else float("nan")
        return cagr, dd, shp, h1, h2, recent

    print(f"\n=== 집중도 격자 PIT (생존편향 제거, {cal[rebal[0]].date()}~{cal[rebal[-1]].date()}) ===")
    print(f"  {'조합':<28s}{'총수익':>9s}{'CAGR':>7s}{'MDD':>7s}{'Sharpe':>7s}{'전17-21':>9s}{'후22-26':>9s}{'최근23-26':>10s}")
    for label, top_n, slot, maxpos in COMBOS:
        final, eqs = run(top_n, slot, maxpos)
        cagr, dd, shp, h1, h2, recent = metrics(eqs, final)
        robust = "✓견고" if (h1 > 0 and h2 > 0) else "✗한쪽음"
        print(f"  {label:<28s}{(final/cap0-1)*100:+8.1f}%{cagr*100:+6.1f}%{dd*100:+6.1f}%{shp:7.2f}"
              f"{h1*100:+8.1f}%{h2*100:+8.1f}%{recent*100:+9.1f}%  {robust} · {time.time()-t0:.0f}s")
    print("\n  ※ 채택 기준: 전·후반 모두 +(견고) AND 인접 조합과 일관(단일 스파이크=과최적 기각) AND MDD 감내 범위.")


if __name__ == "__main__":
    main()
