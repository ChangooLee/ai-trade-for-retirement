"""고확신 집중 진입 검증 — '최고 셋업만 진입하면 승률·기대값이 오르나'를 PIT(생존편향제거)로 실측.
사용자 직감("1년에 몇 번 기회, 그때만 들어가면 고승률") + Byun-Jeon(52주고점). 진입표본의 40일 결과를
RS·국면(D4)·52주고점·눌림깊이 버킷 + 결합('fat pitch')으로 분해. 진입이 결과를 예측하는지 본다.
사용: python -m backtest.conviction_test
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd, yaml
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402
from backtest.early_cut_diagnostic import load_adjusted  # noqa: E402


def stat(s):
    a = s.dropna().values
    if len(a) < 5:
        return None
    return dict(n=len(a), win=float((a > 0).mean()), avg=float(a.mean()),
                med=float(np.median(a)), sd=float(a.std()),
                rr=float(a.mean() / a.std()) if a.std() > 0 else 0.0)


def line(label, st):
    if not st:
        print(f"  {label:26s} (표본<5)"); return
    print(f"  {label:26s} n={st['n']:4d} | 승률 {st['win']:.0%} | 평균 {st['avg']:+.1%} | 중앙 {st['med']:+.1%} | 변동 {st['sd']:.0%} | avg/sd {st['rr']:+.2f}")


def main():
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]; top = 300
    daily = load_adjusted(); index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]; n = len(cal)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal); cls = cls.where(cls > 0)
    by_date = {d: g for d, g in di.groupby("date")}
    HOLD = 40; step = 5; rows = []; prev = set(); ecache = {}
    for i in range(0, n - HOLD - 1, step):
        t = cal[i]; snap = by_date.get(t)
        if snap is None:
            continue
        u = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml)].sort_values("avg_trdval20", ascending=False).head(top)
        if len(u) < 50:
            prev = set(); continue
        wkey = t.to_period("W")
        if wkey not in ecache:
            try:
                ecache[wkey] = compute_d4_exposure(index[index["date"] <= t], t, cfg)["target_exposure"]
            except Exception:
                ecache[wkey] = 0.4
        exp = ecache[wkey]
        lead = compute_leader_flags(u, cfg)
        wa = wk[wk["week_end"] <= last_completed_week_cutoff(t)].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
        pull = compute_pullback_flags(wa, cfg)
        mg = lead.merge(pull[["ticker", "pullback_20w_105", "dist_wma20"]], on="ticker", how="left")
        cands = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)]
        cur = set(cands["ticker"]); ci = cls.iloc[i]
        for _, row in cands[cands["ticker"].isin(cur - prev)].iterrows():
            tk = row["ticker"]; ep = ci.get(tk)
            if not (ep and np.isfinite(ep) and ep > 0):
                continue
            k = i + HOLD; v = cls.iloc[k].get(tk) if k < n else None
            if v and np.isfinite(v) and v > 0:
                r40 = v / ep - 1
            else:
                col = cls[tk].iloc[i:k + 1].dropna(); r40 = (col.iloc[-1] / ep - 1) if len(col) else -1.0
            rows.append(dict(r40=r40, rs=float(row.get("rs_rank") or 0), exp=exp,
                             hi52=float(row["high_52w_ratio"]) if pd.notna(row.get("high_52w_ratio")) else None,
                             dist=float(row["dist_wma20"]) if pd.notna(row.get("dist_wma20")) else None))
        prev = cur
    df = pd.DataFrame(rows)
    print(f"\n=== 고확신 집중 검증 (PIT 생존편향제거 · {cal[0].date()}~{cal[-1].date()} · 진입 {len(df)}건 · 40일 보유수익) ===")
    line("[전체 기준]", stat(df["r40"]))
    print("RS(상대강도):")
    line("RS≥95", stat(df[df.rs >= 95]["r40"])); line("RS 90~95", stat(df[(df.rs >= 90) & (df.rs < 95)]["r40"])); line("RS<90", stat(df[df.rs < 90]["r40"]))
    print("국면(D4 목표노출):")
    line("Risk-On(≥0.6)", stat(df[df.exp >= 0.6]["r40"])); line("중립(0.4~0.6)", stat(df[(df.exp >= 0.4) & (df.exp < 0.6)]["r40"])); line("Risk-Off(<0.4)", stat(df[df.exp < 0.4]["r40"]))
    print("52주고점:")
    line("근접(≥85%)", stat(df[df.hi52 >= 0.85]["r40"])); line("먼(<85%)", stat(df[df.hi52 < 0.85]["r40"]))
    print("눌림 깊이(20주선 이격):")
    line("타이트(≤+2%)", stat(df[df.dist <= 0.02]["r40"])); line("느슨(>+2%)", stat(df[df.dist > 0.02]["r40"]))
    print("결합(fat pitch):")
    line("RS90+RiskOn", stat(df[(df.rs >= 90) & (df.exp >= 0.6)]["r40"]))
    line("RS90+RiskOn+고점근접", stat(df[(df.rs >= 90) & (df.exp >= 0.6) & (df.hi52 >= 0.85)]["r40"]))
    line("RS95+RiskOn+고점+타이트", stat(df[(df.rs >= 95) & (df.exp >= 0.6) & (df.hi52 >= 0.85) & (df.dist <= 0.02)]["r40"]))


if __name__ == "__main__":
    main()
