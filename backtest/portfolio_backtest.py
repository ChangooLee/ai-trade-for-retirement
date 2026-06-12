"""포트폴리오 백테스트 — '우리 매수법'(F리더 ∩ 20주선 눌림)의 실제 수익.

엔진(주간 리밸런스, 실 전략 모듈 재사용):
  진입  F리더 ∩ 20주선 눌림 후보를 D4 변동성 노출(목표노출÷슬롯) 비중으로 익일 시가 매수
  청산  40거래일 시간청산(--tda-exit 시 보유종목 위상 노름 급등도 청산), 익일 시가
  비용  왕복 0.35%(매수·매도 절반씩) · 초기자본 1억
룩어헤드: 신호는 cal[i] 종가까지, 체결은 cal[i+1] 시가. 주봉은 완성주만.
주의: parquet=현재 상장 상위 유니버스라 생존편향(절대수익 낙관). 기준선(유니버스 동일가중) 병기.

사용: python -m backtest.portfolio_backtest [--tda-exit] [--step 5]
"""
from __future__ import annotations
import argparse, math, os, sys, time
import numpy as np, pandas as pd, yaml
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock  # noqa: E402
from app.indicators.tda import stock_topology  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--tda-exit", action="store_true")
    ap.add_argument("--from", dest="frm", default=None, help="시작 YYYYMMDD")
    ap.add_argument("--to", dest="to", default=None, help="종료 YYYYMMDD")
    args = ap.parse_args()
    frm = pd.Timestamp(args.frm) if args.frm else None
    to = pd.Timestamp(args.to) if args.to else None
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    H = cfg["holding"]["max_holding_days"]; cost = cfg["cost"]["assumed_round_trip_cost"]
    cap0 = cfg["portfolio"]["initial_capital"]; maxpos = cfg["sizing"]["max_positions"]
    baseslot = cfg["sizing"]["base_slot_weight"]
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
    P = cfg["tda"]; win, dim, delay = P["window"], P["embed_dim"], P["delay"]

    daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
    index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]; n = len(cal)
    opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal)
    by_date = {d: g for d, g in di.groupby("date")}
    tkc = {tk: (g["date"].values, g["close"].to_numpy(float)) for tk, g in di.sort_values("date").groupby("ticker")}

    def px(mat, i, tk):
        v = mat.iloc[i].get(tk)
        return float(v) if v is not None and np.isfinite(v) and v > 0 else None

    def norm_rising(tk, i):   # 자기참조 위상 노름 급등(직전 20일 평균+1.5σ 초과)?
        dts, c = tkc[tk]; pos = int(np.searchsorted(dts, np.datetime64(cal[i]), side="right") - 1)
        if pos < win + 21: return False
        seg = c[:pos + 1]
        if (seg <= 0).any(): return False
        ret = np.diff(np.log(seg))
        try:
            hist = [stock_topology(ret[-win - k:(-k or None)], dim, delay)[0] for k in range(20, -1, -1)]
        except Exception:
            return False
        cur = hist[-1]; base = hist[:-1]
        m_, s_ = np.mean(base), np.std(base)
        return s_ > 0 and cur > m_ + 1.5 * s_

    cash = float(cap0); pos = {}; eqc = []; trades = []; tlog = []
    bench = []  # 유니버스 동일가중 주간수익(기준선)
    start = 252 + win + 25
    rebal = [i for i in range(start, n - args.step - 1, args.step)
             if (frm is None or cal[i] >= frm) and (to is None or cal[i] <= to)]
    end_idx = max([k for k in range(n) if (to is None or cal[k] <= to)] or [n - 1])
    t0 = time.time()
    for c, i in enumerate(rebal):
        t = cal[i]
        held_val = sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
        eq = cash + held_val
        eqc.append((t, eq))
        # 청산 (익일 시가)
        for tk, p in list(pos.items()):
            held = (i + 1) - p["eidx"]
            tda_sig = args.tda_exit and norm_rising(tk, i)
            if (i - p["eidx"]) >= H or tda_sig:
                sp = px(opn, i + 1, tk)
                if sp is None: continue
                cash += p["sh"] * sp * (1 - cost / 2)
                ret = sp / p["epx"] - 1
                trades.append(ret)
                tlog.append({"entry": str(cal[p["eidx"]].date()), "exit": str(cal[i + 1].date()),
                             "ticker": tk, "name": p["nm"], "hold_d": held, "shares": p["sh"],
                             "entry_px": round(p["epx"]), "exit_px": round(sp),
                             "RS": round(p["rs"]), "high52pct": round(p["h52"] * 100), "dist20w_pct": round(p["dist"] * 100, 1),
                             "exposure": p["mode"], "wt_pct": round(p["wt"] * 100, 1),
                             "reason": "TDA 위상불안정" if tda_sig and (i - p["eidx"]) < H else "40거래일 시간청산",
                             "ret_pct": round(ret * 100, 1), "pnl": round(p["sh"] * (sp - p["epx"]))})
                del pos[tk]
        # D4 노출 + 사이징
        exp = compute_d4_exposure(index, t, cfg); m = exp["target_exposure"]
        slots = compute_target_slots(m, maxpos, baseslot); weight = compute_weight_per_stock(m, slots)
        # 후보 (F리더 ∩ 눌림)
        snap = by_date.get(t)
        if snap is not None and slots > len(pos):
            uni = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml)]
            if len(uni) >= 30:
                lead = compute_leader_flags(uni, cfg)
                wa = wk[wk["week_end"] <= last_completed_week_cutoff(t)].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
                pull = compute_pullback_flags(wa, cfg)
                mg = lead.merge(pull[["ticker", "pullback_20w_105", "dist_wma20"]], on="ticker", how="left")
                cands = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)].sort_values("rs_rank", ascending=False)
                ci = cands.set_index("ticker")
                eq_now = cash + sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
                for tk in cands["ticker"]:
                    if len(pos) >= slots: break
                    if tk in pos: continue
                    bp = px(opn, i + 1, tk)
                    if bp is None: continue
                    sh = math.floor(weight * eq_now / bp)
                    amt = sh * bp * (1 + cost / 2)
                    if sh > 0 and cash >= amt:
                        cash -= amt; r = ci.loc[tk]
                        pos[tk] = {"eidx": i + 1, "epx": bp, "sh": sh, "nm": r.get("name", tk),
                                   "rs": float(r.get("rs_rank") or 0), "h52": float(r.get("high_52w_ratio") or 0),
                                   "dist": float(r.get("dist_wma20") or 0), "mode": exp["mode"], "wt": weight}
        # 기준선: 유니버스 동일가중 다음주 수익
        if c + 1 < len(rebal):
            j = rebal[c + 1]
            u = by_date.get(t)
            if u is not None:
                uu = u[(u["close"] >= mc) & (u["listing_days"] >= ml)]["ticker"]
                rr = [(px(opn, j, tk) / px(opn, i + 1, tk) - 1) for tk in uu
                      if px(opn, i + 1, tk) and px(opn, j, tk)]
                if rr: bench.append(np.mean(rr))

    final_eq = cash + sum(p["sh"] * (px(cls, end_idx, tk) or p["epx"]) for tk, p in pos.items())
    eqdf = pd.DataFrame(eqc, columns=["date", "eq"]).set_index("date")
    rets = eqdf["eq"].pct_change().dropna()
    yrs = (eqc[-1][0] - eqc[0][0]).days / 365.25
    cagr = (final_eq / cap0) ** (1 / yrs) - 1 if yrs > 0 else 0
    dd = (eqdf["eq"] / eqdf["eq"].cummax() - 1).min()
    shp = rets.mean() / rets.std() * math.sqrt(52) if rets.std() > 0 else 0
    twr = (np.prod([1 + r for r in trades]) - 1) if trades else 0
    print(f"\n=== 포트폴리오 백테스트 ({eqc[0][0].date()}~{eqc[-1][0].date()}, 주간, {'TDA청산' if args.tda_exit else '40일 시간청산'}) ===")
    print(f"  초기자본   ₩{cap0:,.0f}")
    print(f"  최종자산   ₩{final_eq:,.0f}")
    print(f"  총수익률   {final_eq/cap0-1:+.1%}   (≈{final_eq/cap0:.2f}배)")
    print(f"  CAGR       {cagr:+.1%}   |  MDD {dd:+.1%}  |  연율변동성 {rets.std()*math.sqrt(52):.1%}  |  Sharpe {shp:.2f}")
    winr = float(np.mean([r > 0 for r in trades])) if trades else 0.0
    print(f"  거래수     {len(trades)}건  승률 {winr:.1%}  평균/건 {np.mean(trades) if trades else 0:+.2%}")
    bench_eq = np.prod([1 + b for b in bench]) if bench else 1
    print(f"  [기준선] 유니버스 동일가중 누적 {bench_eq-1:+.1%} (같은 기간, 비용·노출조절 없음)")
    print("  연도별 수익:")
    for yr, g in eqdf.groupby(eqdf.index.year):
        r = g["eq"].iloc[-1] / g["eq"].iloc[0] - 1
        print(f"    {yr}: {r:+.1%}")
    # 거래 타임라인 로그
    td = pd.DataFrame(tlog)
    suffix = "_tda" if args.tda_exit else ""
    out = f"backtest/trades_timeline{suffix}.csv"
    td.to_csv(out, index=False)
    print(f"\n  === 거래 타임라인 {len(td)}건 → {out} ===")
    print(f"  보유일 분포: 평균 {td['hold_d'].mean():.0f}일 · 중앙 {td['hold_d'].median():.0f}일 · 최소 {td['hold_d'].min()} · 최대 {td['hold_d'].max()}")
    print(f"  청산 사유: " + " · ".join(f"{k} {v}건" for k, v in td['reason'].value_counts().items()))
    print(f"\n  [예시] 거래 타임라인(진입→보유→청산, 앞 16건):")
    s23 = td.sort_values('entry').head(16)
    for _, r in s23.iterrows():
        print(f"    {r['entry']}→{r['exit']} {r['name'][:10]:<10}({r['ticker']}) "
              f"진입 RS{r['RS']}·52주고{r['high52pct']}%·20주선{r['dist20w_pct']:+.0f}% "
              f"@{r['entry_px']:,}→{r['exit_px']:,} {r['hold_d']}일 [{r['reason']}] {r['ret_pct']:+.1f}%")
    print(f"\n  최고 5건:")
    for _, r in td.nlargest(5, 'ret_pct').iterrows():
        print(f"    {r['entry']}→{r['exit']} {r['name'][:10]:<10} {r['hold_d']}일 {r['ret_pct']:+.1f}% (₩{r['pnl']:,})")
    print(f"  최악 5건:")
    for _, r in td.nsmallest(5, 'ret_pct').iterrows():
        print(f"    {r['entry']}→{r['exit']} {r['name'][:10]:<10} {r['hold_d']}일 {r['ret_pct']:+.1f}% (₩{r['pnl']:,})")
    print(f"  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
