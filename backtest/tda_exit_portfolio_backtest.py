"""TDA 청산 규칙 portfolio 백테스트 — 실제 라이브 규칙(횡단면 risk_pct)로 충분히 검증.

생존편향 제거 PIT 패널 · 시총 top400 모멘텀(F리더∩20주선 눌림) · 주간 리밸런스 · D4 노출.
청산 변형(매수·사이징 동일, 청산만 다름):
  time      : 40거래일 시간청산만 (TDA 미사용 — 기준)
  tda_full  : 시간청산 OR 횡단면 risk_pct≥0.75 전량청산 (방향 무관 — 현행 사고)
  tda_gated : 시간청산 OR (risk_pct≥0.75 AND 추세하향) → 0.75~0.85 절반축소·≥0.85 전량 (신규)
각 변형의 CAGR/MDD/Sharpe/승률/평균보유 + 전·후반 분할. TDA는 주간 리밸런스마다 횡단면 1회 계산(캐시).

사용: python -m backtest.tda_exit_portfolio_backtest
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
from app.indicators.tda import compute_tda_signals  # noqa: E402
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock  # noqa: E402
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402

cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
H = cfg["holding"]["max_holding_days"]; cost = cfg["cost"]["assumed_round_trip_cost"]
cap0 = cfg["portfolio"]["initial_capital"]; maxpos = cfg["sizing"]["max_positions"]
baseslot = cfg["sizing"]["base_slot_weight"]; mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
liq = cfg["universe"].get("min_trdval", 5e8); top_n = cfg["universe"]["top_n"]
TP = cfg["tda"]; trim_pct = TP.get("exit_trim_pct", 0.75); full_pct = TP.get("exit_full_pct", 0.85); trim_frac = TP.get("exit_trim_frac", 0.5)

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
frm = pd.Timestamp("20170501")
rebal = [i for i in range(0, n - 6, 5) if cal[i] >= frm]
print(f"공통 지표 {time.time()-t0:.0f}s · 리밸런스 {len(rebal)}회", file=sys.stderr)

def px(mat, i, tk):
    v = mat.iloc[i].get(tk)
    return float(v) if v is not None and np.isfinite(v) and v > 0 else None

def sell_px(i, tk):
    for j in range(i, min(i + 41, n)):
        p = px(opn, j, tk)
        if p is not None:
            return p
    return 0.0

# TDA 횡단면 캐시: date -> {ticker: (risk_pct, trend)}
tda_cache = {}
def tda_at(i, universe_tickers):
    d = cal[i]
    if d in tda_cache:
        return tda_cache[d]
    sub = di[(di["date"] <= d) & (di["ticker"].isin(universe_tickers))]
    try:
        tdf = compute_tda_signals(sub, d, cfg)
        m = {r["ticker"]: (float(r["risk_pct"]), (None if pd.isna(r["trend"]) else float(r["trend"]))) for _, r in tdf.iterrows()} if len(tdf) else {}
    except Exception:
        m = {}
    tda_cache[d] = m
    if len(tda_cache) % 50 == 0:
        print(f"  TDA 캐시 {len(tda_cache)}일 · {time.time()-t0:.0f}s", file=sys.stderr)
    return m

def run(rule):
    cash = float(cap0); pos = {}; eqc = []; trades = []
    for i in rebal:
        t = cal[i]
        held_val = sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
        eqc.append((t, cash + held_val))
        snap = by_date.get(t)
        uni = None
        if snap is not None:
            uni = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml) & (snap.get("avg_trdval20", 0) > liq)]
            uni = uni.sort_values("mktcap", ascending=False).head(top_n)
        tdam = tda_at(i, set(uni["ticker"])) if (rule != "time" and uni is not None and len(pos)) else {}
        # ---- 청산 ----
        for tk, p in list(pos.items()):
            held = i - p["eidx"]
            full = held >= H
            frac = 1.0
            if not full and rule != "time":
                rp, tr = tdam.get(tk, (None, None))
                if rp is not None and rp >= 0.75:
                    if rule == "tda_full":
                        full = True
                    elif rule == "tda_gated" and tr is not None and tr < 0:
                        if rp >= full_pct:
                            full = True
                        elif rp >= trim_pct and not p.get("trimmed"):
                            frac = trim_frac     # 부분 축소
            if full or frac < 1.0:
                sp = sell_px(i + 1, tk)
                if sp > 0:
                    sh = p["sh"] if full else int(p["sh"] * frac)
                    if sh > 0:
                        cash += sh * sp * (1 - cost / 2)
                        trades.append((sp / p["epx"] - 1, held))
                        if full:
                            del pos[tk]
                        else:
                            p["sh"] -= sh; p["trimmed"] = True
        # ---- 매수 (동일 로직) ----
        if uni is None or len(uni) < 50:
            continue
        if t not in ecache:
            ecache[t] = compute_d4_exposure(index[index["date"] <= t], t, cfg)["target_exposure"]
        m = ecache[t]
        slots = compute_target_slots(m, maxpos, baseslot); weight = compute_weight_per_stock(m, slots)
        if slots <= len(pos):
            continue
        lead = compute_leader_flags(uni, cfg)
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
    return final, eqs, trades

def metrics(eqs, final, trades):
    yrs = (eqs.index[-1] - eqs.index[0]).days / 365.25
    cagr = (final / cap0) ** (1 / yrs) - 1
    dd = (eqs / eqs.cummax() - 1).min()
    r = eqs.pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(52) if r.std() > 0 else 0
    mid = pd.Timestamp("20220101")
    e1, e2 = eqs[eqs.index < mid], eqs[eqs.index >= mid]
    h1 = (e1.iloc[-1] / e1.iloc[0] - 1) if len(e1) > 5 else float("nan")
    h2 = (e2.iloc[-1] / e2.iloc[0] - 1) if len(e2) > 5 else float("nan")
    wr = np.mean([1 for x, _ in trades if x > 0]) if trades else 0
    wr = (sum(1 for x, _ in trades if x > 0) / len(trades)) if trades else 0
    hold = np.mean([h for _, h in trades]) if trades else 0
    return cagr, dd, shp, h1, h2, wr, hold, len(trades)

print(f"\n=== TDA 청산 규칙 portfolio 비교 (PIT 시총top{top_n}, {cal[rebal[0]].date()}~{cal[rebal[-1]].date()}) ===")
print(f"  {'규칙':<12s}{'총수익':>9s}{'CAGR':>7s}{'MDD':>7s}{'Sharpe':>7s}{'승률':>6s}{'보유':>6s}{'전17-21':>9s}{'후22-26':>9s}")
for rule in ["time", "tda_full", "tda_gated"]:
    final, eqs, trades = run(rule)
    cagr, dd, shp, h1, h2, wr, hold, nt = metrics(eqs, final, trades)
    print(f"  {rule:<12s}{(final/cap0-1)*100:+8.1f}%{cagr*100:+6.1f}%{dd*100:+6.1f}%{shp:7.2f}{wr*100:5.0f}%{hold:5.0f}일{h1*100:+8.1f}%{h2*100:+8.1f}%  ·{time.time()-t0:.0f}s", flush=True)
print("\n  ※ tda_gated가 time 수익 보존 + tda_full보다 MDD/Sharpe 개선이면 '게이트가 수익 살리며 리스크 관리' 확인.")
