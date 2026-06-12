"""두 방식(모멘텀·추세 / TDA) 매수신호 교집합의 승률·수익률 백테스트.

A 모멘텀·추세 = F리더 ∩ 20주선 눌림 (생산 신호와 동일 조건)
B TDA        = 위상리스크 < 횡단면 중앙값 & 추세(dir) > 0 (생산 tda_buy 조건)
집행          = 신호일 종가 → 익일 시가 진입, 40거래일 후 시가 청산(전략 시간청산과 동일)
룩어헤드      = 신호는 cal[i] 종가까지만, 수익률은 cal[i+1] 이후. 주봉은 완성주만.

주의: parquet은 '현재 상장 상위 유니버스'라 생존편향이 있어 절대수치는 낙관 편향.
      기준선(유니버스 평균) 대비 '초과' 비교로 읽을 것.

사용: python -m backtest.overlap_backtest [--step 5] [--max-dates 0]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import yaml

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.indicators.tda import stock_topology, _wasserstein  # noqa: E402


def _z(s):
    s = pd.to_numeric(s, errors="coerce").astype(float)
    sd = s.std(ddof=0)
    return ((s - s.mean()) / sd).clip(-3, 3) if sd and sd > 0 else s * 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/strategy.yaml")
    ap.add_argument("--step", type=int, default=5, help="리밸런스 간격(거래일)")
    ap.add_argument("--max-dates", type=int, default=0, help="0=전체, n=앞에서 n개만(테스트)")
    ap.add_argument("--out", default="backtest/overlap_trades.csv")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    P = cfg["tda"]; win, dim, delay, lag = P["window"], P["embed_dim"], P["delay"], P["change_lag"]
    H = cfg["holding"]["max_holding_days"]
    minclose, minlist = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]

    daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
    daily_ind = add_daily_indicators(daily)
    weekly_ind = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]
    n = len(cal)
    opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal)
    by_date = {d: g for d, g in daily_ind.groupby("date")}
    tk_close = {tk: (g["date"].values, g["close"].to_numpy(float))
                for tk, g in daily_ind.sort_values("date").groupby("ticker")}

    start = 252 + win + lag + 2
    idxs = list(range(start, n - H - 2, args.step))
    if args.max_dates:
        idxs = idxs[:args.max_dates]
    print(f"리밸런스 {len(idxs)}회 (step={args.step}, hold={H}td) · 유니버스≤{daily['ticker'].nunique()} · "
          f"{cal[idxs[0]].date()}~{cal[idxs[-1]].date()}", file=sys.stderr)

    records = []
    t0 = time.time()
    for c, i in enumerate(idxs):
        d = cal[i]
        snap = by_date.get(d)
        if snap is None:
            continue
        uni = snap[(snap["close"] >= minclose) & (snap["listing_days"] >= minlist)]
        if len(uni) < 30:
            continue
        unitk = list(uni["ticker"])
        # A: 리더 ∩ 눌림
        leaders = compute_leader_flags(uni, cfg)
        wcut = last_completed_week_cutoff(d)
        wa = weekly_ind[weekly_ind["week_end"] <= wcut].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
        pulls = compute_pullback_flags(wa, cfg)
        mg = leaders.merge(pulls[["ticker", "pullback_20w_105"]], on="ticker", how="left")
        setA = set(mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)]["ticker"])
        # B: TDA
        uinfo = uni.set_index("ticker")
        rows = []
        for tk in unitk:
            dts, cl = tk_close[tk]
            pos = int(np.searchsorted(dts, np.datetime64(d), side="right") - 1)
            if pos < win + lag + 1:
                continue
            seg = cl[:pos + 1]
            if (seg <= 0).any():
                continue
            ret = np.diff(np.log(seg))
            try:
                nt, et, h1t = stock_topology(ret[-win:], dim, delay)
                npv, _, h1p = stock_topology(ret[-win - lag:-lag], dim, delay)
            except Exception:
                continue
            r = uinfo.loc[tk]
            ma50 = r["ma50"]; trend = (cl[pos] / ma50 - 1) if ma50 and not pd.isna(ma50) else np.nan
            rows.append((tk, nt, et, _wasserstein(h1p, h1t), nt - npv, r["mom_6m_1m"], trend))
        if not rows:
            continue
        tdf = pd.DataFrame(rows, columns=["ticker", "pl", "ent", "turb", "dnorm", "mom", "trend"])
        tdf["risk"] = (0.7 * (0.5 * _z(tdf["pl"]) + 0.25 * _z(tdf["ent"]) +
                              0.25 * _z(tdf["turb"].fillna(tdf["turb"].median()))) + 0.3 * _z(tdf["dnorm"]))
        tdf["dir"] = 0.6 * _z(tdf["mom"].fillna(tdf["mom"].median())) + 0.4 * _z(tdf["trend"].fillna(tdf["trend"].median()))
        rmed = tdf["risk"].median()
        setB = set(tdf[(tdf["dir"] > 0) & (tdf["risk"] < rmed)]["ticker"])
        # 전방 수익률: 익일 시가 → +H거래일 시가
        entry, exit_ = opn.iloc[i + 1], opn.iloc[i + 1 + H]
        fret = (exit_ / entry - 1).where((entry > 0) & (exit_ > 0))
        for tk in unitk:
            v = fret.get(tk)
            if v is None or not np.isfinite(v):
                continue
            records.append((str(d.date()), tk, tk in setA, tk in setB, float(v)))
        if (c + 1) % 25 == 0:
            print(f"  {c+1}/{len(idxs)} ({d.date()}) 누적표본 {len(records)} · {time.time()-t0:.0f}s", file=sys.stderr)

    R = pd.DataFrame(records, columns=["date", "ticker", "A", "B", "fret"])
    R.to_csv(args.out, index=False)
    print(f"\n총 표본 {len(R)} (date×ticker) · 저장 {args.out} · {time.time()-t0:.0f}s\n", file=sys.stderr)

    def stats(df, label):
        if not len(df):
            print(f"  {label:28s} 표본 0")
            return None
        wr, mr, md = (df["fret"] > 0).mean(), df["fret"].mean(), df["fret"].median()
        print(f"  {label:28s} N={len(df):6d}  승률 {wr:5.1%}  평균 {mr:+6.2%}  중앙 {md:+6.2%}")
        return mr

    base = R["fret"].mean()
    print(f"=== 40거래일 보유 수익률 (생존편향 有, 기준선 대비로 해석) ===")
    stats(R, "유니버스 기준선")
    stats(R[R.A], "A 모멘텀·추세(리더∩눌림)")
    stats(R[R.B], "B TDA(안정+추세)")
    stats(R[R.A & R.B], "★ A∩B 교집합")
    stats(R[R.A & ~R.B], "A only (TDA서 탈락)")
    stats(R[~R.A & R.B], "B only")
    print(f"\n  기준선 평균 {base:+.2%} — 각 군의 '평균-기준선'이 초과수익(엣지).")


if __name__ == "__main__":
    main()
