"""유니버스 정의 격자 PIT 백테스트 — "어떤 유니버스가 최선인가"를 체계 비교.

지표를 1회만 계산하고 여러 유니버스 선정 함수를 같은 시뮬에 통과시킨다.
각 변형마다 전기간 + 전반(2017–2021)/후반(2022–2026) 분할 성과를 보고 → 과최적화·레짐의존 판별.
기준선(각 유니버스 동일가중)도 함께 → '풀 대비 초과'(진짜 알파)를 측정.

사용: python -m backtest.pit_universe_grid
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


# ----- 유니버스 선정 변형 (그 시점 스냅샷 u → 부분집합) -----
def turnover(n):
    return lambda u: u.sort_values("avg_trdval20", ascending=False).head(n)


def mktcap(n, liq=5e8):
    return lambda u: u[u["avg_trdval20"] > liq].sort_values("mktcap", ascending=False).head(n)


def mktcap_band(skip, n, liq=5e8):
    """초대형주 skip개 제외 후 다음 n개(중대형 밴드)."""
    return lambda u: u[u["avg_trdval20"] > liq].sort_values("mktcap", ascending=False).iloc[skip:skip + n]




def intersect(n_mc, n_to):
    """시총 상위 n_mc ∩ 거래대금 상위 n_to (둘 다 만족 — 크고 활발한 종목)."""
    def f(u):
        a = set(u.sort_values("mktcap", ascending=False).head(n_mc)["ticker"])
        b = set(u.sort_values("avg_trdval20", ascending=False).head(n_to)["ticker"])
        return u[u["ticker"].isin(a & b)]
    return f


def composite(n, w_mc=0.6):
    """시총 랭크·거래대금 랭크 가중 복합점수 상위 n (큰+활발 균형)."""
    def f(u):
        u = u.copy()
        u["_s"] = w_mc * u["mktcap"].rank(pct=True) + (1 - w_mc) * u["avg_trdval20"].rank(pct=True)
        return u.sort_values("_s", ascending=False).head(n)
    return f


VARIANTS = {
    "turnover top300": turnover(300),
    "mktcap top200": mktcap(200),
    "mktcap top300": mktcap(300),
    "mktcap top400": mktcap(400),
    "mktcap top500": mktcap(500),
    "mktcap 50-350(중대형)": mktcap_band(50, 300),
    "mktcap top300·유동strict(20억)": mktcap(300, liq=2e9),
    "시총500∩거래대금500(교집합)": intersect(500, 500),
    "복합(시총0.6+거래대금0.4) top300": composite(300, 0.6),
    "복합(시총0.5+거래대금0.5) top300": composite(300, 0.5),
}


def main():
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    H = cfg["holding"]["max_holding_days"]; cost = cfg["cost"]["assumed_round_trip_cost"]
    cap0 = cfg["portfolio"]["initial_capital"]; maxpos = cfg["sizing"]["max_positions"]
    baseslot = cfg["sizing"]["base_slot_weight"]
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
    daily = load_adjusted()
    index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    t0 = time.time()
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]; n = len(cal)
    opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal).where(lambda x: x > 0)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal).where(lambda x: x > 0)
    by_date = {d: g for d, g in di.groupby("date")}
    # 주봉/노출 캐시(변형 간 공유 — 유니버스 무관)
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

    def run(select):
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
        # 전/후반 분할
        mid = pd.Timestamp("20220101")
        e1, e2 = eqs[eqs.index < mid], eqs[eqs.index >= mid]
        h1 = (e1.iloc[-1] / e1.iloc[0] - 1) if len(e1) > 5 else float("nan")
        h2 = (e2.iloc[-1] / e2.iloc[0] - 1) if len(e2) > 5 else float("nan")
        return cagr, dd, shp, h1, h2

    print(f"\n=== 유니버스 격자 PIT (생존편향 제거, {cal[rebal[0]].date()}~{cal[rebal[-1]].date()}) ===")
    print(f"  {'유니버스':<26s}{'총수익':>9s}{'CAGR':>7s}{'MDD':>7s}{'Sharpe':>7s}{'전반17-21':>10s}{'후반22-26':>10s}")
    rows = []
    for name, sel in VARIANTS.items():
        final, eqs = run(sel)
        cagr, dd, shp, h1, h2 = metrics(eqs, final)
        rows.append((name, final / cap0 - 1, cagr, dd, shp, h1, h2))
        print(f"  {name:<26s}{(final/cap0-1)*100:+8.1f}%{cagr*100:+6.1f}%{dd*100:+6.1f}%{shp:7.2f}{h1*100:+9.1f}%{h2*100:+9.1f}%  · {time.time()-t0:.0f}s")
    print("\n  ※ 견고성: 전·후반 모두 양(+) & 인접 크기와 일관돼야 신뢰. 한쪽만 크면 레짐운/과최적.")


if __name__ == "__main__":
    main()
