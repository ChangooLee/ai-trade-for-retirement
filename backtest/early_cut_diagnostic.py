"""정체컷 진단 — '가망없는(정체) 종목을 day K에 끊으면 40일 보유보다 나은가'를 생존편향 없이(PIT) 실측.
리서치(Kaminski-Lo: 컷 효과는 진입후 시계열상관 ρ 부호가 좌우 / Sweeney MAE-MFE / Byun-Jeon 52주고점) 기반.
산출: 시계열상관 ρ(초기 vs 후기), day K 정체(ret≤C) 진입의 '지금컷 vs 40일보유' 비교, 52주고점 조건부.
사용: python -m backtest.early_cut_diagnostic
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd, yaml
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, _REPO)
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402

PIT = os.path.join(_REPO, "data/cache/pit_ohlcv_v2.parquet")


def load_adjusted():
    df = pd.read_parquet(PIT); df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = df.groupby("ticker", group_keys=False); prev = g["close"].shift(1); base = df["close"] - df["chg"]
    r = (prev / base).where((base > 0) & prev.notna(), 1.0)
    r = r.where((r - 1).abs() > 0.005, 1.0); r = r.where(r.between(0.05, 50), 1.0)
    df["_r"] = r
    def ba(s):
        a = s.to_numpy()[::-1]; f = np.cumprod(a)[::-1]
        return pd.Series(np.concatenate([f[1:], [1.0]]), index=s.index)
    df["_f"] = g["_r"].transform(ba)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] / df["_f"]
    print(f"PIT 로드: {df['ticker'].nunique()}종목 · {df['date'].nunique()}일", file=sys.stderr)
    return df.drop(columns=["_r", "_f", "chg"])


def main():
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]; top = 300
    daily = load_adjusted()
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]; n = len(cal)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal); cls = cls.where(cls > 0)
    by_date = {d: g for d, g in di.groupby("date")}
    KS = [10, 15, 20]; HOLD = 40; step = 5
    rows = []; prev = set()
    for i in range(0, n - HOLD - 1, step):
        t = cal[i]; snap = by_date.get(t)
        if snap is None:
            continue
        u = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml)].sort_values("avg_trdval20", ascending=False).head(top)
        if len(u) < 50:
            prev = set(); continue
        lead = compute_leader_flags(u, cfg)
        wa = wk[wk["week_end"] <= last_completed_week_cutoff(t)].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
        pull = compute_pullback_flags(wa, cfg)
        mg = lead.merge(pull[["ticker", "pullback_20w_105"]], on="ticker", how="left")
        cands = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)]
        cur = set(cands["ticker"]); new = cur - prev      # 에피소드 시작(직전 리밸런스에 없던 후보)
        ci = cls.iloc[i]
        for tk in new:
            ep = ci.get(tk)
            if not (ep and np.isfinite(ep) and ep > 0):
                continue
            r = {}
            for K in KS + [HOLD]:
                k = i + K
                if k >= n:
                    r[f"r{K}"] = None; continue
                v = cls.iloc[k].get(tk)
                if v and np.isfinite(v) and v > 0:
                    r[f"r{K}"] = v / ep - 1
                else:                                       # 상폐/결측 → 구간 내 마지막 유효가(손실 반영)
                    col = cls[tk].iloc[i:k + 1].dropna()
                    r[f"r{K}"] = (col.iloc[-1] / ep - 1) if len(col) else -1.0
            hr = snap[snap["ticker"] == tk]["high_52w_ratio"]
            r["hi52"] = float(hr.iloc[0]) if len(hr) else None
            rows.append(r)
        prev = cur
    df = pd.DataFrame(rows)
    print(f"\n=== 정체컷 진단 (PIT 생존편향제거 · {str(cal[0].date())}~{str(cal[-1].date())} · 진입 {len(df)}건) ===")
    print("진입후 시계열상관 ρ — 양수=모멘텀 지속(정체컷이 미래 러너 자름·손해) / 0·음수=컷 유효 가능:")
    for K in KS:
        sub = df.dropna(subset=[f"r{K}", "r40"]); late = sub["r40"] - sub[f"r{K}"]
        rho = float(np.corrcoef(sub[f"r{K}"], late)[0, 1]) if len(sub) > 5 else float("nan")
        print(f"   ρ(0~{K}일 수익 vs {K}~40일 수익) = {rho:+.3f}")
    print("\n정체 진입(day K에 ret≤C)의 운명 — '지금 컷' vs '40일 보유':")
    for K in KS:
        for C in (0.0, 0.01, 0.03):
            sub = df.dropna(subset=[f"r{K}", "r40"]); flat = sub[sub[f"r{K}"] <= C]
            if len(flat) < 5:
                continue
            cull = flat[f"r{K}"].mean(); hold = flat["r40"].mean()
            print(f"   K={K:2d} C={C:+.0%}: 정체 {len(flat):3d}건 → 지금컷 {cull:+.1%} vs 보유 {hold:+.1%} "
                  f"(보유−컷 {hold - cull:+.1%}, {'보유우위' if hold > cull else '컷우위'}) · 보유시 +비율 {(flat['r40'] > 0).mean():.0%}")
    K = 15
    sub = df.dropna(subset=[f"r{K}", "r40", "hi52"]); flat = sub[sub[f"r{K}"] <= 0.01]
    near = flat[flat["hi52"] >= 0.85]; far = flat[flat["hi52"] < 0.85]
    print("\n52주고점 조건부 (K=15·정체 ret≤+1% 진입 중):")
    if len(near) > 3:
        print(f"   고점근접(≥85%) {len(near)}건 → 40일 {near['r40'].mean():+.1%} (+비율 {(near['r40']>0).mean():.0%})")
    if len(far) > 3:
        print(f"   고점 먼(<85%)  {len(far)}건 → 40일 {far['r40'].mean():+.1%} (+비율 {(far['r40']>0).mean():.0%})")


if __name__ == "__main__":
    main()
