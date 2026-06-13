"""리스크·사이징 변형 비교 백테스트 (생존편향 제거 PIT 패널).

사용자 질문: 현행(A=월 −3% 서킷브레이커 차단 + 시간40일·20주선 청산, 등가중) 대비
  (B) 종목별 하드 손절가, (C) 발동 시 전량 청산형 서킷브레이커, 그리고
  사이징을 등가중 대신 '가능성 좋은 종목(RS)'에 더 싣는 컨빅션 방식이 수익·MDD·Sharpe·승률에 어떤가?

설계(공정 비교): 진입 시그널(F리더∩20주선 눌림, RS순)·추세이탈(20주선) sells·D4 노출은 한 번만 precompute
(build_bt_archive와 동일 로직) → 각 변형은 동일 진입 위에서 '청산 규칙'과 '비중'만 바꿔 재생한다.
가격: PIT 조정가(상폐 포함). 진입=리밸런스 익일 시가. 시간/추세/브레이커=리밸런스(주 1회) 점검.
종목 손절(B)=일 단위 저가로 점검(갭다운 시 시가 체결). 왕복비용 config.

검증: --only PURE 는 20주선·브레이커·손절 모두 끄고 시간청산만 → pit_mktcap_backtest(+155.7%) 재현.
사용: .venv/bin/python -m backtest.risk_sizing_variants [--top 400] [--step 5] [--only NAME,NAME]
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
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402


def precompute(top, step, frm):
    """리밸런스별 시그널 1회 계산: buy[(tk,rs_rank)] · sells(20주선 이탈) · m(D4 목표노출). 가격행렬도 반환."""
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    mc, ml = cfg["universe"]["min_close"], cfg["universe"]["min_listing_days"]
    liq = cfg["universe"].get("min_trdval", 5e8)
    maxpos = cfg["sizing"]["max_positions"]; baseslot = cfg["sizing"]["base_slot_weight"]
    daily = load_adjusted(); index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    t0 = time.time()
    di = add_daily_indicators(daily)
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    cal = [pd.Timestamp(d) for d in trading_calendar(daily)]
    n = len(cal)
    opn = daily.pivot_table(index="date", columns="ticker", values="open").reindex(cal).where(lambda x: x > 0)
    cls = daily.pivot_table(index="date", columns="ticker", values="close").reindex(cal).where(lambda x: x > 0)
    low = daily.pivot_table(index="date", columns="ticker", values="low").reindex(cal).where(lambda x: x > 0)
    low_roll20 = low.rolling(20, min_periods=5).min()           # 스윙로우(20일 저점) — 트레일링 손절용
    by_date = {d: g for d, g in di.groupby("date")}
    print(f"지표 계산 {time.time()-t0:.0f}s · {len(cal)}거래일", file=sys.stderr)

    frm = pd.Timestamp(frm)
    rebal = [i for i in range(0, n - step - 1, step) if cal[i] >= frm]
    sig = {}; wcache = {}
    for c, i in enumerate(rebal):
        t = cal[i]; snap = by_date.get(t)
        if snap is None:
            sig[i] = {"buy": [], "sells": set(), "m": 0.4}; continue
        uni = snap[(snap["close"] >= mc) & (snap["listing_days"] >= ml) & (snap.get("avg_trdval20", 0) > liq)]
        uni = uni.sort_values("mktcap", ascending=False).head(top)
        if len(uni) < 50:
            sig[i] = {"buy": [], "sells": set(), "m": 0.4}; continue
        try: m = compute_d4_exposure(index[index["date"] <= t], t, cfg)["target_exposure"]
        except Exception: m = 0.4
        lead = compute_leader_flags(uni, cfg); cut = last_completed_week_cutoff(t)
        if cut not in wcache:
            wcache[cut] = wk[wk["week_end"] <= cut].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
        pull = compute_pullback_flags(wcache[cut], cfg)
        mg = lead.merge(pull[["ticker", "pullback_20w_105", "w_ma20"]], on="ticker", how="left")
        cand = mg[mg["is_f_leader"] & mg["pullback_20w_105"].fillna(False)].sort_values("rs_rank", ascending=False)
        buy = [(r["ticker"], float(r.get("rs_rank", 0) or 0)) for _, r in cand.iterrows()]
        mg["bw"] = mg["close"] / mg["w_ma20"] - 1
        sells = set(mg[(mg["bw"] < 0) & (mg["mom_6m_1m"] < 0)]["ticker"])
        sig[i] = {"buy": buy, "sells": sells, "m": float(m), "slots_in": maxpos, "base": baseslot}
        if (c + 1) % 100 == 0:
            print(f"  precompute {c+1}/{len(rebal)} ({t.date()}) {time.time()-t0:.0f}s", file=sys.stderr)
    meta = {"cal": cal, "n": n, "rebal": rebal, "opn": opn, "cls": cls, "low": low,
            "low_roll20": low_roll20, "maxpos": maxpos, "baseslot": baseslot,
            "cost": cfg["cost"]["assumed_round_trip_cost"], "cap0": cfg["portfolio"]["initial_capital"], "H": cfg["holding"]["max_holding_days"]}
    return sig, meta


def _val(mat, i, tk):
    try: v = mat.iat[i, mat.columns.get_loc(tk)]
    except KeyError: return None
    return float(v) if v is not None and np.isfinite(v) and v > 0 else None


def run_variant(name, cfg_v, sig, meta, i0=None, i1=None):
    """한 변형 시뮬. i0/i1(cal 인덱스)로 기간 제한(워크포워드용) — 그 구간만 신규자본으로 시뮬."""
    cal, n, rebal = meta["cal"], meta["n"], meta["rebal"]
    opn, cls, low, low_roll20 = meta["opn"], meta["cls"], meta["low"], meta["low_roll20"]
    H, cost, cap0 = meta["H"], meta["cost"], meta["cap0"]
    maxpos, baseslot = meta["maxpos"], meta["baseslot"]
    trend = cfg_v.get("trend", True); breaker = cfg_v.get("breaker", "block"); cb = cfg_v.get("cb", 0.03)
    stop = cfg_v.get("stop"); sizing = cfg_v.get("sizing", "equal")

    cash = float(cap0); pos = {}; trades = []; writeoffs = 0
    eq_daily = np.full(n, np.nan); cb_month = None; cb_base = 0.0
    rebal_set = {i: c for c, i in enumerate(rebal)}
    rin = [i for i in rebal if (i0 is None or i >= i0) and (i1 is None or i <= i1)]
    start_i = rin[0] if rin else rebal[0]
    end_i = i1 if i1 is not None else (n - 1)

    def equity_at(i):
        h = 0.0
        for tk, p in pos.items():
            v = _val(cls, i, tk) or _val(opn, i, tk) or p["epx"]
            h += p["sh"] * v
        return cash + h

    def close_pos(tk, price, i, reason):
        nonlocal cash
        p = pos.pop(tk)
        if price <= 0:
            trades.append({"ret": -1.0, "reason": "상폐전손"}); return
        cash += p["sh"] * price * (1 - cost / 2)
        trades.append({"ret": price / p["epx"] - 1, "reason": reason})

    for i in range(start_i, end_i + 1):
        t = cal[i]; mon = (t.year, t.month)
        # --- 일 단위 종목 손절(B) — 저가 이탈 시 갭다운 인지 체결 ---
        if stop:
            for tk in list(pos.keys()):
                p = pos[tk]
                if i <= p["eidx"]:
                    continue
                lo = _val(low, i, tk)
                if lo is None:
                    continue
                if stop[0] == "swing":
                    sw = _val(low_roll20, i - 1, tk)            # 전일까지의 20일 저점(룩어헤드 차단)
                    if sw and sw > p["stop"]:
                        p["stop"] = sw                          # 트레일링 상향만
                stp = p["stop"]
                if stp and lo <= stp:
                    op = _val(opn, i, tk)
                    fill = op if (op is not None and op <= stp) else stp   # 갭다운이면 시가 체결
                    close_pos(tk, fill, i, "손절")
        # --- 리밸런스: 시간/추세 청산 → 브레이커 → 진입 ---
        if i in rebal_set:
            # 청산: 시간 40거래일 OR 추세이탈(20주선)
            sells = sig[i]["sells"] if trend else set()
            for tk in list(pos.keys()):
                p = pos[tk]
                hit_time = (i - p["eidx"]) >= H
                hit_trend = tk in sells
                if hit_time or hit_trend:
                    sp = _val(opn, min(i + 1, n - 1), tk) or _val(cls, i, tk)
                    if sp is None:                              # 호가 없음 → 이후 40일 첫 시가, 없으면 전손
                        for j in range(i + 1, min(i + 41, n)):
                            sp = _val(opn, j, tk)
                            if sp: break
                    if not sp:
                        writeoffs += 1; pos.pop(tk); trades.append({"ret": -1.0, "reason": "상폐전손"}); continue
                    close_pos(tk, sp, i, "시간청산" if hit_time else "추세이탈")
            # 브레이커: 당월 손익(누적손익 − 월초 누적손익) ≤ −cb×원금
            eq_now = equity_at(i); cur_pnl = eq_now - cap0
            if cb_month != mon:
                cb_month, cb_base = mon, cur_pnl
            tripped = cb > 0 and (cur_pnl - cb_base) <= -cb * cap0
            if tripped and breaker == "liq":                    # 전량 청산형
                for tk in list(pos.keys()):
                    sp = _val(opn, min(i + 1, n - 1), tk) or _val(cls, i, tk)
                    if sp: close_pos(tk, sp, i, "브레이커청산")
            # 진입 (미발동 & 슬롯 여유)
            block = tripped and breaker in ("block", "liq")
            m = sig[i]["m"]; slots = compute_target_slots(m, maxpos, baseslot)
            base_w = compute_weight_per_stock(m, slots)
            buy = [b for b in sig[i]["buy"] if b[0] not in pos]
            if not block and slots > len(pos) and buy and base_w > 0:
                eq_now = equity_at(i); need = slots - len(pos)
                # 컨빅션 배수: 의도한 상위 need개 후보의 RS로 [0.5,1.7]배·합 보존(나머지는 1.0)
                mult_by = {}
                if sizing == "conviction":
                    head = buy[:need]
                    if len(head) > 1:
                        rs = np.array([max(1.0, b[1]) for b in head]); mu = rs / rs.mean()
                        mu = np.clip(mu, 0.5, 1.7); mu *= len(head) / mu.sum()
                        mult_by = {head[k][0]: float(mu[k]) for k in range(len(head))}
                ni = i + 1 if i + 1 < n else i
                for tk, _rs in buy:                      # 후보를 순서대로 채워 slots 충족(미체결분은 다음 후보로) — 베이스라인과 동일
                    if len(pos) >= slots: break
                    bp = _val(opn, ni, tk)
                    if bp is None: continue
                    w = base_w * mult_by.get(tk, 1.0)
                    sh = math.floor(w * eq_now / bp)
                    amt = sh * bp * (1 + cost / 2)
                    if sh > 0 and cash >= amt:
                        cash -= amt
                        p = {"eidx": ni, "epx": bp, "sh": sh}
                        if stop:
                            p["stop"] = bp * (1 - stop[1]) if stop[0] == "pct" else (_val(low_roll20, i, tk) or bp * 0.85)
                        pos[tk] = p
        eq_daily[i] = equity_at(i)

    # 종료 평가
    final_eq = equity_at(end_i)
    eq = pd.Series(eq_daily).dropna()
    yrs = max(1e-6, (cal[end_i] - cal[start_i]).days / 365.25)
    cagr = (final_eq / cap0) ** (1 / yrs) - 1 if final_eq > 0 else -1
    dd = float((eq / eq.cummax() - 1).min())
    r = eq.pct_change().dropna()
    shp = float(r.mean() / r.std() * math.sqrt(252)) if r.std() > 0 else 0.0
    rets = [x["ret"] for x in trades]
    winr = float(np.mean([x > 0 for x in rets])) if rets else 0.0
    yearly = {}
    eqy = pd.Series(eq.values, index=[cal[k] for k in range(start_i, end_i + 1) if np.isfinite(eq_daily[k])][:len(eq)])
    for yr, g in eqy.groupby(eqy.index.year):
        yearly[yr] = g.iloc[-1] / g.iloc[0] - 1
    return {"name": name, "total": final_eq / cap0 - 1, "cagr": cagr, "mdd": dd, "sharpe": shp,
            "ntr": len(trades), "win": winr, "avg": float(np.mean(rets)) if rets else 0.0,
            "writeoffs": writeoffs, "final": final_eq, "yearly": yearly}


VARIANTS = {
    "PURE(검증:시간청산만)":      {"trend": False, "breaker": "none", "cb": 0, "stop": None, "sizing": "equal"},
    "A(현행:시간+20주선+월-3%차단)": {"trend": True, "breaker": "block", "cb": 0.03, "stop": None, "sizing": "equal"},
    "A2(20주선만·브레이커끔)":     {"trend": True, "breaker": "none", "cb": 0, "stop": None, "sizing": "equal"},
    "A3(월-3%차단만·20주선끔)":    {"trend": False, "breaker": "block", "cb": 0.03, "stop": None, "sizing": "equal"},
    "B1(A+손절-8%)":             {"trend": True, "breaker": "block", "cb": 0.03, "stop": ("pct", 0.08), "sizing": "equal"},
    "B2(A+손절-12%)":            {"trend": True, "breaker": "block", "cb": 0.03, "stop": ("pct", 0.12), "sizing": "equal"},
    "B3(A+스윙로우트레일)":        {"trend": True, "breaker": "block", "cb": 0.03, "stop": ("swing",), "sizing": "equal"},
    "C(브레이커=전량청산)":        {"trend": True, "breaker": "liq", "cb": 0.03, "stop": None, "sizing": "equal"},
    "S(A+컨빅션사이징)":          {"trend": True, "breaker": "block", "cb": 0.03, "stop": None, "sizing": "conviction"},
    "C+B3(전량청산+스윙로우)":     {"trend": True, "breaker": "liq", "cb": 0.03, "stop": ("swing",), "sizing": "equal"},
    "C+S(전량청산+컨빅션)":        {"trend": True, "breaker": "liq", "cb": 0.03, "stop": None, "sizing": "conviction"},
    "C+B3+S(전부결합)":          {"trend": True, "breaker": "liq", "cb": 0.03, "stop": ("swing",), "sizing": "conviction"},
    # 임계값 민감도(헤드라인 검증: 차단 vs 전량청산 × 한도)
    "차단-2%":  {"trend": True, "breaker": "block", "cb": 0.02, "stop": None, "sizing": "equal"},
    "차단-5%":  {"trend": True, "breaker": "block", "cb": 0.05, "stop": None, "sizing": "equal"},
    "청산-2%":  {"trend": True, "breaker": "liq", "cb": 0.02, "stop": None, "sizing": "equal"},
    "청산-5%":  {"trend": True, "breaker": "liq", "cb": 0.05, "stop": None, "sizing": "equal"},
    "청산-7%":  {"trend": True, "breaker": "liq", "cb": 0.07, "stop": None, "sizing": "equal"},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=400); ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--from-eq", dest="frm", default="20170501")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    sig, meta = precompute(a.top, a.step, a.frm)
    names = [s.strip() for s in a.only.split(",") if s.strip()] or list(VARIANTS)
    rows = []
    for nm in names:
        key = next((k for k in VARIANTS if k == nm or k.startswith(nm)), None)
        if not key: print(f"  ? 알 수 없는 변형: {nm}", file=sys.stderr); continue
        t0 = time.time(); res = run_variant(key, VARIANTS[key], sig, meta)
        rows.append(res); print(f"  ✓ {key} {time.time()-t0:.1f}s", file=sys.stderr)
    print("\n=== 리스크·사이징 변형 비교 (PIT 생존편향 제거, top%d, 2017~2026) ===" % a.top)
    print(f"{'변형':<28}{'총수익':>9}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}{'승률':>7}{'거래':>6}")
    for r in rows:
        print(f"{r['name']:<28}{r['total']:>+8.1%}{r['cagr']:>+8.1%}{r['mdd']:>+8.1%}{r['sharpe']:>8.2f}{r['win']:>7.1%}{r['ntr']:>6}")
    print("\n연도별 수익률:")
    yrs = sorted({y for r in rows for y in r["yearly"]})
    print(f"{'변형':<28}" + "".join(f"{y:>8}" for y in yrs))
    for r in rows:
        print(f"{r['name']:<28}" + "".join(f"{r['yearly'].get(y, float('nan')):>+8.1%}" if y in r['yearly'] else f"{'—':>8}" for y in yrs))


if __name__ == "__main__":
    main()
