"""TDA 청산 규칙 비교 — 방향 게이트(추세하향 동시)가 '승자 조기청산'을 줄이는지 검증.

매수=모멘텀(F리더∩눌림) 고정. 청산 규칙:
  time   : 40거래일 시간청산(기준)
  tda    : 보유 중 노름이 직전 20일평균+1.5σ 초과(위상 불안정 급등) → 청산  [현행 사고]
  gated  : 위 불안정 급등 AND 그날 종가<MA50(추세 하향)일 때만 청산, 아니면 시간청산  [방향 게이트]
지표: 수익/승률/보유일 + **승자 조기청산율**(시간청산이 +였는데 규칙이 더 일찍 더 낮게 나간 비율)
      + **하락 회피**(시간청산이 −였던 거래에서 규칙이 손실을 줄였는지).
생존편향 有(현재 유니버스), 진입 step=20, 룩어헤드 차단.
"""
from __future__ import annotations
import os, sys, time
import numpy as np, pandas as pd, yaml
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
from app.data.calendar import trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.indicators.tda import stock_topology  # noqa: E402

cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
P = cfg["tda"]; win, dim, delay = P["window"], P["embed_dim"], P["delay"]
H = cfg["holding"]["max_holding_days"]; BASE = 20; K = 1.5
mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
di = add_daily_indicators(daily)
wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
cal = [pd.Timestamp(d) for d in trading_calendar(daily)]; n = len(cal)
opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal)
by_date = {d: g for d, g in di.groupby("date")}
tkc = {tk: (g["date"].values, g["close"].to_numpy(float), g["ma50"].to_numpy(float))
       for tk, g in di.sort_values("date").groupby("ticker")}

entries = []
for i in range(252 + win + BASE + 2, n - H - 2, 20):
    d = cal[i]; snap = by_date.get(d)
    if snap is None: continue
    uni = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml)]
    if len(uni) < 30: continue
    lead = compute_leader_flags(uni, cfg)
    wa = wk[wk["week_end"] <= last_completed_week_cutoff(d)].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
    pull = compute_pullback_flags(wa, cfg)
    mg = lead.merge(pull[["ticker", "pullback_20w_105"]], on="ticker", how="left")
    for tk in mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)]["ticker"]:
        entries.append((i, tk))
print(f"모멘텀 진입 이벤트 {len(entries)}건 (step=20)", file=sys.stderr)

ncache = {}
def at(tk, idx):
    """cal[idx] 시점의 (노름, 종가, ma50). 룩어헤드 없음."""
    key = (tk, idx)
    if key in ncache: return ncache[key]
    dts, cl, ma = tkc[tk]
    pos = int(np.searchsorted(dts, np.datetime64(cal[idx]), side="right") - 1)
    norm = np.nan; close = np.nan; m50 = np.nan
    if pos >= 0:
        close = cl[pos]; m50 = ma[pos]
        if pos >= win + 1 and not (cl[:pos + 1] <= 0).any():
            ret = np.diff(np.log(cl[:pos + 1]))
            try: norm = stock_topology(ret[-win:], dim, delay)[0]
            except Exception: norm = np.nan
    out = (norm, close, m50); ncache[key] = out; return out

def px(tk, idx):
    try:
        v = opn.iloc[idx].get(tk)
        return float(v) if v and np.isfinite(v) and v > 0 else np.nan
    except Exception: return np.nan

t0 = time.time(); rows = []
for c, (i, tk) in enumerate(entries):
    ep = px(tk, i + 1)
    if not np.isfinite(ep): continue
    series = [at(tk, i + off) for off in range(-BASE, H + 1)]   # entry at index BASE
    norms = [s[0] for s in series]
    off_tda = H; off_gated = H
    found_tda = found_gated = False
    for d in range(1, H + 1):
        trail = [x for x in norms[d:BASE + d] if np.isfinite(x)]
        cur_norm, cur_close, cur_ma = series[BASE + d]
        if len(trail) >= 10 and np.isfinite(cur_norm):
            m_, s_ = np.mean(trail), np.std(trail)
            spike = s_ > 0 and cur_norm > m_ + K * s_
            if spike and not found_tda:
                off_tda = d; found_tda = True
            downtrend = np.isfinite(cur_close) and np.isfinite(cur_ma) and cur_ma > 0 and cur_close < cur_ma
            if spike and downtrend and not found_gated:
                off_gated = d; found_gated = True
        if found_tda and found_gated:
            break
    xp_t = px(tk, i + 1 + H)
    xp_x = px(tk, i + 1 + off_tda)
    xp_g = px(tk, i + 1 + off_gated)
    if not (np.isfinite(xp_t) and np.isfinite(xp_x) and np.isfinite(xp_g)): continue
    rows.append((str(cal[i].date()), tk, ep, xp_t / ep - 1, xp_x / ep - 1, xp_g / ep - 1, off_tda, off_gated))
    if (c + 1) % 60 == 0:
        print(f"  {c+1}/{len(entries)} · {time.time()-t0:.0f}s", file=sys.stderr)

R = pd.DataFrame(rows, columns=["date", "ticker", "entry", "ret_time", "ret_tda", "ret_gated", "off_tda", "off_gated"])
R.to_csv("backtest/tda_gated_exit_trades.csv", index=False)

def stat(col, off, lab):
    f = R[col]
    print(f"  {lab:16s} N={len(R)} 승률{(f>0).mean():5.1%} 평균{f.mean():+6.2%} 중앙{f.median():+6.2%} σ{f.std():5.1%} 위험조정{f.mean()/f.std():+.3f} 최악{f.min():+6.1%} 보유{R[off].mean():4.1f}일")

print(f"\n=== TDA 청산 규칙 비교 (N={len(R)}, {time.time()-t0:.0f}s) — 매수=모멘텀 고정 ===")
stat("ret_time", None, "40일 시간청산"); print(f"  {'':16s} (보유 {H}일 고정)")
stat("ret_tda", "off_tda", "TDA 급등청산")
stat("ret_gated", "off_gated", "방향게이트청산")
# 승자 조기청산 / 하락 회피 분석
win_t = R["ret_time"] > 0
cut_tda = win_t & (R["off_tda"] < H) & (R["ret_tda"] < R["ret_time"])     # 승자였는데 더 일찍 더 낮게
cut_g = win_t & (R["off_gated"] < H) & (R["ret_gated"] < R["ret_time"])
loss_t = R["ret_time"] < 0
avoid_tda = loss_t & (R["ret_tda"] > R["ret_time"])                        # 손실거래서 손실 축소
avoid_g = loss_t & (R["ret_gated"] > R["ret_time"])
print(f"\n  [승자 조기청산율]  TDA {cut_tda.sum()}/{win_t.sum()} ({cut_tda.mean()*100:.0f}% of all)  vs  게이트 {cut_g.sum()}/{win_t.sum()} ({cut_g.mean()*100:.0f}%)")
print(f"  [승자 평균 손해(놓친 수익)]  TDA {(R.loc[cut_tda,'ret_tda']-R.loc[cut_tda,'ret_time']).mean()*100:+.1f}%p  vs  게이트 {(R.loc[cut_g,'ret_gated']-R.loc[cut_g,'ret_time']).mean()*100:+.1f}%p")
print(f"  [하락 회피(손실거래 손실축소)]  TDA {avoid_tda.sum()}/{loss_t.sum()} ({avoid_tda.mean()*100:.0f}%)  vs  게이트 {avoid_g.sum()}/{loss_t.sum()} ({avoid_g.mean()*100:.0f}%)")
print(f"  조기청산 비율: TDA {(R['off_tda']<H).mean():.0%} vs 게이트 {(R['off_gated']<H).mean():.0%}")
