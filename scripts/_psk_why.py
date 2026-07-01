"""031980(피에스케이홀딩스) 6월 주별 매수조건 재현 — 왜 1주차 아니고 5주차에 추천됐나.
(A) 주간 종가 vs 20주선 / 눌림 플래그  (B) 각 주 asof 전체 파이프라인(F리더·RS·52주고점·눌림·최종매수).
사용: .venv/bin/python scripts/_psk_why.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd, yaml
from app.data import krx_loader as L
from app.data.calendar import resolve_asof, last_completed_week_cutoff
from app.indicators.daily import add_daily_indicators
from app.indicators.weekly import to_weekly, add_weekly_indicators
from app.indicators.leader import compute_leader_flags
from app.indicators.pullback import compute_pullback_flags
from app.portfolio import ledger

TK = "031980"
cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
daily_all = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
lb = cfg["pullback"]["low_band"]; wma = cfg["pullback"]["weekly_ma"]

# (A) 주간 시계열
wk_all = add_weekly_indicators(to_weekly(daily_all), wma, lb)
wk = wk_all[wk_all["ticker"] == TK].copy()
wk = wk[(wk["week_end"] >= "2026-04-15") & (wk["week_end"] <= "2026-07-10")]
print(f"=== (A) 031980 주간: 종가 vs 20주선 · 눌림조건(주중저가 ≤ 20주선×{lb} & 종가>20주선) ===")
print(f"  {'주말':11s} {'종가':>9s} {'주중저가':>9s} {'20주선':>9s} {'종가-MA':>8s} {'저가-MA':>8s}  눌림")
for _, r in wk.iterrows():
    d2 = r["close"] / r["w_ma20"] - 1 if pd.notna(r["w_ma20"]) else float("nan")
    l2 = r["low"] / r["w_ma20"] - 1 if pd.notna(r["w_ma20"]) else float("nan")
    print(f"  {str(r['week_end'].date()):11s} {r['close']:>9,.0f} {r['low']:>9,.0f} {r['w_ma20']:>9,.0f} {d2:>+7.1%} {l2:>+7.1%}  {'✅눌림' if r['pullback_20w_105'] else '—'}")

# (B) 각 asof 전체 파이프라인
print(f"\n=== (B) 각 주 asof — 매수 3조건 (F리더 ∩ 눌림 ∩ 52주고점≥{cfg['pullback']['min_high52w_ratio']}) ===")
print(f"  {'asof':11s} {'F리더':>6s} {'RS순위':>6s} {'52주고점':>8s} {'눌림':>5s} {'→최종매수':>8s}")
for a in ["20260605", "20260612", "20260619", "20260626", "20260701"]:
    daily = daily_all[daily_all["date"] <= pd.Timestamp(a)].copy()
    if daily.empty:
        print(f"  {a}: 데이터 없음"); continue
    asof, _ = resolve_asof(a, daily)
    daily = daily[daily["date"] <= asof]
    daily_ind = add_daily_indicators(daily)
    weekly_ind = add_weekly_indicators(to_weekly(daily), wma, lb)
    daily_asof = daily_ind.sort_values(["ticker", "date"]).groupby("ticker").tail(1).copy()
    wk_cut = last_completed_week_cutoff(asof)
    weekly_asof = weekly_ind[weekly_ind["week_end"] <= wk_cut].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
    uni = daily_asof[(daily_asof["close"] >= cfg["universe"]["min_close"]) &
                     (daily_asof["listing_days"] >= cfg["universe"]["min_listing_days"])].copy()
    leaders = compute_leader_flags(uni, cfg)
    pulls = compute_pullback_flags(weekly_asof, cfg)
    merged = leaders.merge(pulls[["ticker", "pullback_20w_105", "dist_wma20", "w_ma20"]], on="ticker", how="left")
    cand = ledger.build_buy_candidates(merged, pd.DataFrame(), pd.DataFrame(), cfg)
    inbuy = (TK in set(cand["ticker"])) if len(cand) else False
    row = merged[merged["ticker"] == TK]
    if len(row):
        r = row.iloc[0]
        print(f"  {str(asof.date()):11s} {('✅' if r.get('is_f_leader') else '—'):>6s} {r.get('rs_rank', float('nan')):>6.0f} "
              f"{r.get('high_52w_ratio', float('nan')):>8.2f} {('✅' if r.get('pullback_20w_105') else '—'):>5s} {('✅ 매수' if inbuy else '—'):>8s}")
    else:
        print(f"  {str(asof.date()):11s} (유니버스 외)")
