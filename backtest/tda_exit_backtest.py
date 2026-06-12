"""매수=모멘텀(F리더∩눌림) 고정, 청산 규칙 비교: 40거래일 시간청산 vs TDA 불안정 청산.

TDA 청산: 보유 중 종목의 지속성 풍경 노름이 자기 직전 20일 평균+1.5σ를 넘으면(위상 불안정
급등 = Gidea-Katz 위기 전조) 다음 거래일 시가 청산. 미발생 시 40거래일에 시간청산.
→ "매도를 TDA로" 했을 때 수익/승률/보유일/변동성/최악손실이 어떻게 바뀌는지 검증.

생존편향 有(현재 유니버스). 진입 step=20거래일로 표본 중첩 완화. 룩어헤드 차단.
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
tkc = {tk: (g["date"].values, g["close"].to_numpy(float)) for tk, g in di.sort_values("date").groupby("ticker")}

# 진입 이벤트 수집 (A = 리더 ∩ 눌림)
entries = []
for i in range(252 + win + BASE + 2, n - H - 2, 20):
    d = cal[i]; snap = by_date.get(d)
    if snap is None: continue
    uni = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml)]
    if len(uni) < 30: continue
    lead = compute_leader_flags(uni, cfg)
    wa = wk[wk["week_end"] <= last_completed_week_cutoff(d)].sort_values(["ticker","week_end"]).groupby("ticker").tail(1)
    pull = compute_pullback_flags(wa, cfg)
    mg = lead.merge(pull[["ticker","pullback_20w_105"]], on="ticker", how="left")
    for tk in mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)]["ticker"]:
        entries.append((i, tk))
print(f"모멘텀 진입 이벤트 {len(entries)}건 (step=20)", file=sys.stderr)

cache = {}
def norm_at(tk, idx):
    key = (tk, idx)
    if key in cache: return cache[key]
    dts, cl = tkc[tk]
    pos = int(np.searchsorted(dts, np.datetime64(cal[idx]), side="right") - 1)
    v = np.nan
    if pos >= win + 1 and not (cl[:pos+1] <= 0).any():
        ret = np.diff(np.log(cl[:pos+1]))
        try: v = stock_topology(ret[-win:], dim, delay)[0]
        except Exception: v = np.nan
    cache[key] = v; return v

def px(tk, idx):
    try:
        v = opn.iloc[idx].get(tk)
        return float(v) if v and np.isfinite(v) and v > 0 else np.nan
    except Exception: return np.nan

t0 = time.time(); rows = []
for c, (i, tk) in enumerate(entries):
    ep = px(tk, i + 1)
    if not np.isfinite(ep): continue
    norms = [norm_at(tk, i + off) for off in range(-BASE, H + 1)]   # idx -BASE..+H, entry at index BASE
    exit_off = H
    for d in range(1, H + 1):
        trail = [x for x in norms[d:BASE + d] if np.isfinite(x)]    # 직전 BASE일 노름
        cur = norms[BASE + d]
        if len(trail) >= 10 and np.isfinite(cur):
            m_, s_ = np.mean(trail), np.std(trail)
            if s_ > 0 and cur > m_ + K * s_:
                exit_off = d; break
    xp_t = px(tk, i + 1 + H)        # 시간청산
    xp_x = px(tk, i + 1 + exit_off) # TDA청산
    if not np.isfinite(xp_t) or not np.isfinite(xp_x): continue
    rows.append((str(cal[i].date()), tk, ep, xp_t / ep - 1, xp_x / ep - 1, exit_off))
    if (c + 1) % 50 == 0:
        print(f"  {c+1}/{len(entries)} · {time.time()-t0:.0f}s", file=sys.stderr)

R = pd.DataFrame(rows, columns=["date","ticker","entry","ret_time","ret_tda","hold"])
R.to_csv("backtest/tda_exit_trades.csv", index=False)
def s(col, lab):
    f = R[col]; print(f"  {lab:18s} N={len(R)} 승률{(f>0).mean():5.1%} 평균{f.mean():+6.2%} 중앙{f.median():+6.2%} σ{f.std():5.1%} 위험조정{f.mean()/f.std():+.3f} 최악{f.min():+6.1%}")
print(f"\n=== 매수=모멘텀 고정, 청산 규칙 비교 (N={len(R)}, {time.time()-t0:.0f}s) ===")
s("ret_time", "40일 시간청산")
s("ret_tda", "TDA 불안정청산")
print(f"  평균 보유일: 시간청산 {H} vs TDA청산 {R['hold'].mean():.1f}일 (조기청산 {(R['hold']<H).mean():.0%})")
