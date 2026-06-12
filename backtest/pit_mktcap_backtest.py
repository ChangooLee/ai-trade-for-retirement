"""생존편향 없는(point-in-time) 포트폴리오 백테스트 — 모멘텀·추세 전략 재검증.

데이터: data/cache/pit_ohlcv.parquet (공식 API 일별 전종목 백필 — 상폐 종목 포함).
보정:  분할/감자 = (종가-대비)=조정기준가 로 이벤트 비율 검출 → 과거 OHLC 역보정.
유니버스: 매 리밸런스 시점의 20일 평균 거래대금 상위 300 (그 시점 기준 — 미래 정보 없음).
전략:  생산 코드 재사용 — F리더 ∩ 20주선 눌림(완성주), D4 노출 사이징, 익일 시가 집행,
       40거래일 시간청산, 왕복 0.35%.
상폐:  청산 시점에 호가 없으면 이후 40거래일 내 첫 유효 시가로 매도, 그래도 없으면 전손(-100%).
기준선: 같은 PIT 유니버스 동일가중 — 생존편향 크기도 함께 측정.

사용: python -m backtest.pit_portfolio_backtest [--step 5] [--from-eq 20170501]
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

PIT = os.path.join(_REPO, "data/cache/pit_ohlcv_v2.parquet")   # mktcap 포함


def load_adjusted():
    """PIT 패널 로드 + 분할/감자 역보정(수정주가 재구성)."""
    df = pd.read_parquet(PIT)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = df.groupby("ticker", group_keys=False)
    prev_close = g["close"].shift(1)
    base = df["close"] - df["chg"]                      # 조정기준가
    ratio = (prev_close / base).where((base > 0) & prev_close.notna(), 1.0)
    ratio = ratio.where((ratio - 1).abs() > 0.005, 1.0) # 틱 허용오차
    ratio = ratio.where(ratio.between(0.05, 50), 1.0)   # 이상치 가드
    df["_ratio"] = ratio
    def back_adjust(s):
        r = s.to_numpy()[::-1]
        f = np.cumprod(r)[::-1]
        return pd.Series(np.concatenate([f[1:], [1.0]]), index=s.index)   # Π_{s>t} ratio
    df["_f"] = g["_ratio"].transform(back_adjust)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] / df["_f"]
    n_events = int((df["_ratio"] != 1.0).sum())
    df = df.drop(columns=["_ratio", "_f", "chg"])  # mktcap 유지
    print(f"PIT 로드: {df['ticker'].nunique()}종목 · {df['date'].nunique()}일 · {len(df)}행 · "
          f"기업행위 이벤트 보정 {n_events}건", file=sys.stderr)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--from-eq", dest="frm", default="20170501")
    ap.add_argument("--top", type=int, default=300)
    ap.add_argument("--universe", choices=["turnover", "mktcap"], default="mktcap",
                    help="유니버스 선정 기준: 거래대금(turnover) vs 시가총액(mktcap)")
    args = ap.parse_args()
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
    print(f"지표 계산 {time.time()-t0:.0f}s", file=sys.stderr)
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]
    n = len(cal)
    opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal)
    opn = opn.where(opn > 0)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal)
    cls = cls.where(cls > 0)
    by_date = {d: g for d, g in di.groupby("date")}

    def px(mat, i, tk):
        v = mat.iloc[i].get(tk)
        return float(v) if v is not None and np.isfinite(v) and v > 0 else None

    def sell_px(i, tk):
        """청산가: i일 시가, 없으면 이후 40일 내 첫 유효 시가, 그래도 없으면 0(전손)."""
        for j in range(i, min(i + 41, n)):
            p = px(opn, j, tk)
            if p is not None:
                return p, j
        return 0.0, None

    frm = pd.Timestamp(args.frm)
    rebal = [i for i in range(0, n - args.step - 1, args.step) if cal[i] >= frm]
    cash = float(cap0); pos = {}; eqc = []; trades = []; writeoffs = 0
    bench = []
    print(f"리밸런스 {len(rebal)}회 {cal[rebal[0]].date()}~{cal[rebal[-1]].date()}", file=sys.stderr)
    for cidx, i in enumerate(rebal):
        t = cal[i]
        held = sum(p["sh"] * (px(cls, i, tk) or px(cls, max(i - 5, 0), tk) or 0) for tk, p in pos.items())
        eqc.append((t, cash + held))
        # 청산
        for tk, p in list(pos.items()):
            if (i - p["eidx"]) >= H:
                sp, _ = sell_px(i + 1, tk)
                if sp <= 0:
                    writeoffs += 1; trades.append(-1.0); del pos[tk]; continue
                cash += p["sh"] * sp * (1 - cost / 2)
                trades.append(sp / p["epx"] - 1)
                del pos[tk]
        # PIT 유니버스 (그 시점 상위 300)
        snap = by_date.get(t)
        if snap is None: continue
        u = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml)].copy()
        if args.universe == "mktcap":
            u = u[u["avg_trdval20"] > 5e8]                   # 체결현실성 최소 거래대금 5억
            u = u.sort_values("mktcap", ascending=False).head(args.top)
        else:
            u = u.sort_values("avg_trdval20", ascending=False).head(args.top)
        if len(u) < 50: continue
        exp = compute_d4_exposure(index[index["date"] <= t], t, cfg)
        slots = compute_target_slots(exp["target_exposure"], maxpos, baseslot)
        weight = compute_weight_per_stock(exp["target_exposure"], slots)
        if slots > len(pos):
            lead = compute_leader_flags(u, cfg)
            wa = wk[wk["week_end"] <= last_completed_week_cutoff(t)].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
            pull = compute_pullback_flags(wa, cfg)
            mg = lead.merge(pull[["ticker", "pullback_20w_105"]], on="ticker", how="left")
            cands = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)].sort_values("rs_rank", ascending=False)
            eq_now = cash + sum(p["sh"] * (px(cls, i, tk) or p["epx"]) for tk, p in pos.items())
            for tk in cands["ticker"]:
                if len(pos) >= slots: break
                if tk in pos: continue
                bp = px(opn, i + 1, tk)
                if bp is None: continue
                sh = math.floor(weight * eq_now / bp)
                amt = sh * bp * (1 + cost / 2)
                if sh > 0 and cash >= amt:
                    cash -= amt; pos[tk] = {"eidx": i + 1, "epx": bp}
                    pos[tk]["sh"] = sh
        # PIT 기준선(동일가중, 다음 리밸런스까지)
        if cidx + 1 < len(rebal):
            j = rebal[cidx + 1]
            rr = []
            for tk in u["ticker"]:
                a, b = px(opn, i + 1, tk), px(opn, j, tk)
                if a and b: rr.append(b / a - 1)
                elif a: rr.append(-1.0)        # 상폐/정지 — 기준선에도 전손 반영
            if rr: bench.append(np.mean(rr))
        if (cidx + 1) % 50 == 0:
            print(f"  {cidx+1}/{len(rebal)} ({t.date()}) eq={eqc[-1][1]/1e8:.2f}억 · {time.time()-t0:.0f}s", file=sys.stderr)

    end_idx = n - 1
    final_eq = cash + sum(p["sh"] * (px(cls, end_idx, tk) or 0) for tk, p in pos.items())
    eqdf = pd.DataFrame(eqc, columns=["date", "eq"]).set_index("date")
    yrs = (eqc[-1][0] - eqc[0][0]).days / 365.25
    cagr = (final_eq / cap0) ** (1 / yrs) - 1
    dd = (eqdf["eq"] / eqdf["eq"].cummax() - 1).min()
    r = eqdf["eq"].pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(52) if r.std() > 0 else 0
    winr = float(np.mean([x > 0 for x in trades])) if trades else 0
    bench_cum = float(np.prod([1 + b for b in bench])) - 1 if bench else 0
    print(f"\n=== PIT(생존편향 제거) 백테스트 [유니버스={args.universe}] {eqc[0][0].date()}~{cal[end_idx].date()} ===")
    print(f"  최종자산   ₩{final_eq:,.0f}  (총 {final_eq/cap0-1:+.1%})")
    print(f"  CAGR {cagr:+.1%} | MDD {dd:+.1%} | Sharpe {shp:.2f}")
    print(f"  거래 {len(trades)}건 · 승률 {winr:.1%} · 평균 {np.mean(trades) if trades else 0:+.2%} · 상폐 전손 {writeoffs}건")
    print(f"  [PIT 기준선] 시점별 상위{args.top} 동일가중 누적 {bench_cum:+.1%}")
    print("  연도별:")
    for yr, g in eqdf.groupby(eqdf.index.year):
        print(f"    {yr}: {g['eq'].iloc[-1]/g['eq'].iloc[0]-1:+.1%}")


if __name__ == "__main__":
    main()
