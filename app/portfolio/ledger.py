"""포지션 원장 (§12,13,31) — 매도/검토/보유뷰/매수후보. run_daily는 읽기전용."""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from app.data.calendar import trading_days_between, nth_trading_day_from  # noqa: E402


def _open(positions):
    if len(positions) and "status" in positions.columns:
        return positions[positions["status"] == "open"]
    return positions


def _shares(p, initial_capital):
    """수량. 없으면 entry_weight×자본/진입가로 환산."""
    s = p.get("shares")
    if s is not None and s == s and float(s) > 0:
        return float(s)
    ew, ep = float(p.get("entry_weight") or 0), float(p.get("entry_price") or 0)
    return (ew * initial_capital / ep) if ep else 0.0


def portfolio_accounting(positions, daily_asof, initial_capital):
    """수량×현재가 기반 실제 비중·현금·총자산 회계."""
    info = daily_asof.set_index("ticker")
    per, invested_cost, holdings_value = {}, 0.0, 0.0
    for _, p in _open(positions).iterrows():
        tk = p["ticker"]
        sh = _shares(p, initial_capital)
        cost = sh * float(p.get("entry_price") or 0)
        cur = float(info.loc[tk, "close"]) if tk in info.index else np.nan
        val = sh * cur if cur == cur else np.nan
        per[tk] = {"shares": sh, "cost": cost, "cur": cur, "value": val}
        invested_cost += cost
        holdings_value += val if val == val else 0.0
    realized = 0.0
    if len(positions) and "status" in positions.columns:
        for _, p in positions[positions["status"] == "closed"].iterrows():
            if p.get("exit_price") == p.get("exit_price") and p.get("entry_price"):
                realized += _shares(p, initial_capital) * (float(p["exit_price"]) - float(p["entry_price"]))
    cash = initial_capital - invested_cost + realized
    total = cash + holdings_value
    for tk in per:
        v = per[tk]["value"]
        per[tk]["weight"] = (v / total) if (total > 0 and v == v) else np.nan
    return {"cash": cash, "total_equity": total, "holdings_value": holdings_value,
            "invested_cost": invested_cost, "realized_pnl": realized, "per_ticker": per}


def build_sell_candidates(positions, daily_asof, cal, asof, config):
    maxd = config["holding"]["max_holding_days"]
    info = daily_asof.set_index("ticker")
    sells, reviews = [], []
    for _, p in _open(positions).iterrows():
        tk = p["ticker"]
        hd = trading_days_between(cal, p["entry_date"], asof)
        cur = float(info.loc[tk, "close"]) if tk in info.index else np.nan
        risk = float(info.loc[tk, "swing_low20"]) if tk in info.index else np.nan
        ret = cur / p["entry_price"] - 1 if (cur == cur and p["entry_price"]) else np.nan
        rec = dict(ticker=tk, name=p.get("name", tk), market=p.get("market", ""),
                   entry_date=p["entry_date"], entry_price=p["entry_price"],
                   entry_weight=p.get("entry_weight"), holding_days=hd, cur=cur, ret=ret, risk=risk)
        if hd >= maxd:
            rec["reason"] = "40거래일 경과 (검증된 청산)"
            sells.append(rec)
        elif cur == cur and risk == risk and cur < risk:
            rec["reason"] = "스윙로우(20일) 이탈 — 참고위험선, 청산 검토"
            reviews.append(rec)
    return pd.DataFrame(sells), pd.DataFrame(reviews)


def simulate_positions_after_sells(positions, sells):
    op = _open(positions)
    if not len(op):
        return op
    sold = set(sells["ticker"]) if len(sells) else set()
    return op[~op["ticker"].isin(sold)]


def build_positions_view(positions, daily_asof, cal, asof, config, acct=None):
    maxd = config["holding"]["max_holding_days"]
    info = daily_asof.set_index("ticker")
    per = (acct or {}).get("per_ticker", {})
    rows = []
    for _, p in _open(positions).iterrows():
        tk = p["ticker"]
        hd = trading_days_between(cal, p["entry_date"], asof)
        rem = max(0, maxd - hd)
        cur = float(info.loc[tk, "close"]) if tk in info.index else np.nan
        risk = float(info.loc[tk, "swing_low20"]) if tk in info.index else np.nan
        ret = cur / p["entry_price"] - 1 if (cur == cur and p["entry_price"]) else np.nan
        planned = nth_trading_day_from(cal, p["entry_date"], maxd)
        a = per.get(tk, {})
        weight = a.get("weight", p.get("entry_weight"))
        shares = a.get("shares")
        value = a.get("value", np.nan)
        cost = a.get("cost", np.nan)
        pnl_won = (value - cost) if (value == value and cost == cost) else np.nan
        if tk not in info.index:
            status, dday = "가격없음", ""
        elif hd >= maxd:
            status, dday = "매도예정", "D0"
        elif cur == cur and risk == risk and cur < risk:
            status, dday = "검토", f"D-{rem}"
        elif rem <= 5:
            status, dday = "40거래일 임박", f"D-{rem}"
        else:
            status, dday = "정상", f"D-{rem}"
        rows.append(dict(ticker=tk, name=p.get("name", tk), market=p.get("market", ""),
                         entry_date=p["entry_date"], entry_price=p["entry_price"],
                         weight=weight, shares=shares, value=value, cost=cost, pnl_won=pnl_won,
                         holding_days=hd, remaining=rem, ret=ret, cur=cur, risk=risk,
                         planned_exit=planned, status=status, dday=dday))
    return pd.DataFrame(rows)


def build_buy_candidates(merged, positions_after_sells, sells, config):
    held = set(positions_after_sells["ticker"]) if len(positions_after_sells) else set()
    sold = set(sells["ticker"]) if len(sells) else set()
    cand = merged[merged["is_f_leader"] & merged["pullback_20w_105"].fillna(False)].copy()
    cand = cand[~cand["ticker"].isin(held | sold)]
    if not len(cand):
        return cand
    cand["trdval_rank"] = cand["avg_trdval20"].rank(pct=True)
    cand["pull_rank"] = 1 - cand["dist_wma20"].abs().rank(pct=True)
    cand["leader_score"] = (0.45 * cand["rs_rank"] / 100 + 0.25 * cand["high_52w_ratio"].clip(0, 1)
                            + 0.20 * cand["trdval_rank"] + 0.10 * cand["pull_rank"]) * 100
    return cand.sort_values(["leader_score", "rs_rank", "avg_trdval20"], ascending=False)
