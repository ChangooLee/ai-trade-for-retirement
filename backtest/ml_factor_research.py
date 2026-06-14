"""ML·팩터 리서치 (qlib식 기능을 우리 PIT 하버스에 이식) — 사용자 요청.

목적: "ML 모델이 우리 모멘텀 룰을 이기나?" + qlib식 IC 분석·팩터셋·리포트.
- 팩터셋: Alpha158 부분집합(수익률·MA비·변동성·거래량·위치·모멘텀 ~15종), PIT 패널에서 계산.
- IC 분석: 각 팩터의 횡단면 rank-IC(팩터 vs 전방수익률) 평균·ICIR — '어떤 팩터가 KRX 수익을 예측하나'.
- ML 벤치마크: 워크포워드 LightGBM(분기 재학습) → 전방수익 예측 → 주간 top-k 롱온리 백테스트.
  비교군: 모멘텀 top-k(RS 랭크) · 동일가중 전체(벤치) · (참고) 우리 실제 전략 +193%.
- 생존편향 제거 PIT(top400) · 주간 리밸런스 · 왕복비용 0.35% · 룩어헤드 차단(전방수익은 미래라 학습 라벨로만).

사용: .venv/bin/python -m backtest.ml_factor_research [--quick] [--topk 8] [--fwd 5]
주의: 연구용. 주간 리밸런스 하버스(실엔진=일단위와 다를 수 있음).
"""
from __future__ import annotations
import argparse, sys, time
import numpy as np, pandas as pd, yaml
sys.path.insert(0, ".")
from app.data.calendar import trading_calendar  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402

FACTORS = ["ret5", "ret10", "ret20", "ret60", "cma5", "cma20", "cma60",
           "vol20", "vol60", "vratio", "pos20", "pos60", "amihud20", "mom_6m_1m", "rs60"]


def compute_factors(df):
    """Alpha158 부분집합 — 지표 프레임(add_daily_indicators) 위에 가격/거래량 팩터 추가. transform으로 정렬 보장."""
    df = df.sort_values(["ticker", "date"]).copy()
    def gt(col, fn): return df.groupby("ticker", group_keys=False)[col].transform(fn)
    def gp(w): return df.groupby("ticker", group_keys=False)["close"].pct_change(w, fill_method=None)
    df["_lr"] = gt("close", lambda s: np.log(s).diff())
    for w in (5, 10, 20, 60):
        df[f"ret{w}"] = gp(w)
    for w in (5, 20, 60):
        ma = gt("close", lambda s: s.rolling(w, min_periods=max(2, w // 2)).mean())
        df[f"cma{w}"] = df["close"] / ma - 1
    for w in (20, 60):
        df[f"vol{w}"] = gt("_lr", lambda s: s.rolling(w, min_periods=w // 2).std())
        hi = gt("high", lambda s: s.rolling(w, min_periods=w // 2).max())
        lo = gt("low", lambda s: s.rolling(w, min_periods=w // 2).min())
        df[f"pos{w}"] = (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    vmean = gt("volume", lambda s: s.rolling(20, min_periods=10).mean())
    df["vratio"] = df["volume"] / vmean.replace(0, np.nan)
    df["_il"] = df["_lr"].abs() / (df["close"] * df["volume"]).replace(0, np.nan)
    df["amihud20"] = gt("_il", lambda s: s.rolling(20, min_periods=10).mean()) * 1e9
    df["rs60"] = df["ret60"]
    if "mom_6m_1m" not in df.columns:
        df["mom_6m_1m"] = gp(105) - gp(21)
    return df


def build_panel(daily, top, step, frm, fwd):
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
    liq = cfg["universe"].get("min_trdval", 5e8)
    t0 = time.time()
    di = add_daily_indicators(daily)                           # listing_days·avg_trdval20·mom_6m_1m·mktcap 등
    print(f"지표 계산 {time.time()-t0:.0f}s", file=sys.stderr)
    df = compute_factors(di)
    # 전방 수익(라벨): 다음 fwd일 단순수익 — 미래값이라 학습 라벨로만(예측엔 미사용=룩어헤드 없음)
    df["fwd"] = df.groupby("ticker", group_keys=False)["close"].transform(lambda s: s.shift(-fwd) / s - 1)
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]
    frm = pd.Timestamp(frm)
    reb = [d for k, d in enumerate(cal) if k % step == 0 and d >= frm]
    byd = {d: x for d, x in df.groupby("date")}
    rows = []
    for d in reb:
        snap = byd.get(d)
        if snap is None:
            continue
        u = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml) & (snap.get("avg_trdval20", 0) > liq)]
        u = u.sort_values("mktcap", ascending=False).head(top)
        if len(u) < 50:
            continue
        rows.append(u.assign(rebal=d))
    panel = pd.concat(rows, ignore_index=True)
    print(f"패널 구성 {len(reb)}리밸런스·{len(panel)}행 · {time.time()-t0:.0f}s", file=sys.stderr)
    return panel, sorted(panel["rebal"].unique())


def ic_table(panel):
    """팩터별 횡단면 rank-IC(팩터 vs 전방수익) 평균·ICIR·승률."""
    out = []
    for f in FACTORS:
        ics = []
        for d, g in panel.groupby("rebal"):
            sub = g[[f, "fwd"]].dropna()
            if len(sub) >= 30:
                ics.append(sub[f].rank().corr(sub["fwd"].rank()))
        ics = np.array([x for x in ics if np.isfinite(x)])
        if len(ics):
            out.append({"factor": f, "IC": ics.mean(), "ICIR": ics.mean() / ics.std() if ics.std() else 0,
                        "IC>0%": (ics > 0).mean(), "n": len(ics)})
    return pd.DataFrame(out).sort_values("IC", key=lambda s: s.abs(), ascending=False)


def topk_backtest(panel, rebals, score_col, topk, cost=0.0035):
    """주간 top-k 롱온리 등가중: score_col 상위 topk 보유 → 다음 리밸런스 fwd수익. 회전비용 반영."""
    eq = 1.0; curve = []; prev = set(); rets = []
    for d in rebals:
        g = panel[panel["rebal"] == d]
        sub = g[[ "ticker", score_col, "fwd"]].dropna()
        if len(sub) < topk:
            curve.append((d, eq)); continue
        pick = set(sub.sort_values(score_col, ascending=False).head(topk)["ticker"])
        r = g[g["ticker"].isin(pick)]["fwd"].mean()
        turn = len(pick ^ prev) / (2 * topk) if prev else 1.0       # 교체율
        r = (r if np.isfinite(r) else 0) - turn * cost
        eq *= (1 + r); rets.append(r); prev = pick
        curve.append((d, eq))
    return curve, np.array(rets)


def stats(curve, rets, periods_per_yr):
    eq = pd.Series([e for _, e in curve])
    total = eq.iloc[-1] - 1
    yrs = len(curve) / periods_per_yr
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    mdd = float((eq / eq.cummax() - 1).min())
    shp = rets.mean() / rets.std() * np.sqrt(periods_per_yr) if rets.std() else 0
    win = (rets > 0).mean() if len(rets) else 0
    return total, cagr, mdd, shp, win


def ml_walk_forward(panel, rebals, topk, retrain_every, cost):
    import lightgbm as lgb
    feats = FACTORS
    pdf = panel.dropna(subset=["fwd"]).copy()
    eq = 1.0; curve = []; prev = set(); rets = []; model = None
    rb_idx = {d: i for i, d in enumerate(rebals)}
    for i, d in enumerate(rebals):
        # 분기마다 과거(현재 리밸런스 이전, fwd가 실현된 데이터만) 재학습 — 룩어헤드 차단
        if i >= 26 and (model is None or i % retrain_every == 0):
            cutoff = rebals[i]
            # fwd가 실현되려면 학습 라벨의 리밸런스가 충분히 과거여야 함(fwd 구간 확보)
            tr = pdf[pdf["rebal"] < rebals[max(0, i - 1)]]
            tr = tr.dropna(subset=feats + ["fwd"])
            if len(tr) > 2000:
                model = lgb.LGBMRegressor(n_estimators=200, num_leaves=31, learning_rate=0.03,
                                          subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
                                          n_jobs=2, verbose=-1)
                model.fit(tr[feats], tr["fwd"])
        g = panel[panel["rebal"] == d]
        sub = g.dropna(subset=feats).copy()
        if model is None or len(sub) < topk:
            curve.append((d, eq)); continue
        sub["pred"] = model.predict(sub[feats])
        pick = set(sub.sort_values("pred", ascending=False).head(topk)["ticker"])
        r = g[g["ticker"].isin(pick)]["fwd"].mean()
        turn = len(pick ^ prev) / (2 * topk) if prev else 1.0
        r = (r if np.isfinite(r) else 0) - turn * cost
        eq *= (1 + r); rets.append(r); prev = pick
        curve.append((d, eq))
    return curve, np.array(rets), model, feats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=400); ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--topk", type=int, default=8); ap.add_argument("--fwd", type=int, default=5)
    ap.add_argument("--from-eq", dest="frm", default="20170501")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    if a.quick:
        a.frm = "20230101"
    daily = load_adjusted()
    panel, rebals = build_panel(daily, a.top, a.step, a.frm, a.fwd)
    ppy = 252 / a.step                                          # 연 환산(주간≈50.4)
    retrain = max(4, int(ppy / 4))                              # 분기 재학습

    print("\n=== IC 분석 (팩터 vs %d일 전방수익, 횡단면 rank-IC) ===" % a.fwd)
    ic = ic_table(panel)
    print(ic.to_string(index=False, float_format=lambda x: f"{x:+.4f}"))

    print("\n=== top-%d 롱온리 백테스트 (주간 리밸런스, PIT top%d, 비용 0.35%%) ===" % (a.topk, a.top))
    print(f"{'전략':<22}{'총수익':>9}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}{'승률':>7}")
    # 벤치: 전체 동일가중(점수=상수면 turnover 0 가정 위해 mktcap 상위로 고정picK 대신 전체평균)
    bench_rets = np.array([panel[panel["rebal"] == d]["fwd"].dropna().mean() for d in rebals])
    bench_rets = np.nan_to_num(bench_rets)
    beq = np.cumprod(1 + bench_rets); bcurve = list(zip(rebals, beq))
    for nm, (cv, rt) in {
        "동일가중 전체(벤치)": (bcurve, bench_rets),
        "모멘텀 top-k(ret60)": topk_backtest(panel, rebals, "ret60", a.topk),
        "모멘텀 top-k(mom6-1)": topk_backtest(panel, rebals, "mom_6m_1m", a.topk),
    }.items():
        t_, c_, m_, s_, w_ = stats(cv, rt, ppy)
        print(f"{nm:<22}{t_:>+8.1%}{c_:>+8.1%}{m_:>+8.1%}{s_:>8.2f}{w_:>7.1%}")
    t0 = time.time()
    cv, rt, model, feats = ml_walk_forward(panel, rebals, a.topk, retrain, 0.0035)
    t_, c_, m_, s_, w_ = stats(cv, rt, ppy)
    print(f"{'ML LightGBM top-k':<22}{t_:>+8.1%}{c_:>+8.1%}{m_:>+8.1%}{s_:>8.2f}{w_:>7.1%}   (학습 {time.time()-t0:.0f}s)")
    if model is not None:
        imp = sorted(zip(feats, model.feature_importances_), key=lambda x: -x[1])
        print("  ML 팩터 중요도 top8:", ", ".join(f"{f}({v})" for f, v in imp[:8]))
    print("\n참고) 우리 실제 전략(모멘텀 발굴+시간40/20주선, 브레이커끔)은 같은 PIT top400서 +193%/MDD-37%/Sharpe0.70.")


if __name__ == "__main__":
    main()
