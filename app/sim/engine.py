"""알고리즘 페이퍼 시뮬레이터 엔진 — 하루치 전진(순수 함수, 무의존성).

라이브 화면이 추천하는 전략(시총 top400 F리더∩20주선 눌림 + 40거래일/TDA 청산 + D4 노출 + 월 −3% 서킷브레이커)을
사용자별 가상 포트폴리오에 매일 1스텝씩 집행한다. 백테스트의 전진 1일 버전.

execute_day(state, day, sig) → 새 state(현금·포지션·서킷브레이커 앵커) + 그날 체결·에쿼티.
  state: {investment, cash, positions[], cb_month, cb_base_pnl}
    positions[i]: {ticker,name,entry_date,entry_price,shares,last_price}
  sig (daily_signals.json 한 줄): {hold_days, cost, exposure:{slots,weight}, buy_order:[{ticker,name,close}],
                                    sell_tickers:[...], prices:{tk:close}, calendar:[YYYY-MM-DD,...]}
규칙:
  1) 청산: 보유 40거래일 경과 OR 그날 매도신호(20주선이탈/TDA) → 종가 매도(왕복비용 절반씩).
  2) 서킷브레이커: 당월 손익 ≤ −3%×투자금 → 그달 신규매수 중단(보유는 청산규칙 유지).
  3) 매수: D4 목표슬롯 미달 & 미발동 시 buy_order에서 미보유 종목을 비중×에쿼티로 종가 매수(현금 한도).
  4) 평가: 잔여 포지션 종가 평가 → 에쿼티 기록.
실행가: 시그널일 종가(배치 EOD). 백테스트(익일 시가)와 미세차 — 상한 해석 아닌 보수적 근사.
"""
from __future__ import annotations
import math

CB_LIMIT = 0.03


def _trading_days_between(cal, d1, d2):
    """달력(YYYY-MM-DD 오름차순)에서 d1 < d ≤ d2 인 거래일 수."""
    return sum(1 for d in cal if d1 < d <= d2)


def _price(sig, tk, fallback):
    p = sig.get("prices", {}).get(tk)
    return float(p) if p and p > 0 else (float(fallback) if fallback else 0.0)


def _equity(cash, positions, sig):
    hv = 0.0
    for p in positions:
        px = _price(sig, p["ticker"], p.get("last_price") or p["entry_price"])
        hv += p["shares"] * px
    return cash + hv, hv


def execute_day(state, day, sig):
    """하루치 집행. (new_state, result) 반환. result: {date,equity,cash,holdings_value,trades[],tripped}."""
    inv = float(state["investment"])
    cash = float(state["cash"])
    positions = [dict(p) for p in state.get("positions", [])]
    cost = float(sig.get("cost", 0.0035))
    cal = sig.get("calendar", [])
    hold_days = int(sig.get("hold_days", 40))
    sells_set = set(sig.get("sell_tickers", []))     # 추세이탈(20주선) — 전량청산
    trades = []

    # 보유 종목 최신가 갱신(가능하면)
    for p in positions:
        px = sig.get("prices", {}).get(p["ticker"])
        if px and px > 0:
            p["last_price"] = float(px)

    def _record_sell(p, sh, px, held, reason):
        proceeds = sh * px * (1 - cost / 2)
        buy_cost = p["entry_price"] * sh * (1 + cost / 2)
        trades.append({"ticker": p["ticker"], "name": p.get("name", p["ticker"]),
                       "entry_date": p["entry_date"], "entry_price": p["entry_price"],
                       "exit_date": day, "exit_price": px, "shares": sh,
                       "pnl": round(proceeds - buy_cost), "ret": (px / p["entry_price"] - 1) if p["entry_price"] else 0.0,
                       "days": held, "reason": reason})
        return proceeds

    # 1) 청산 — 40거래일 시간청산 OR 20주선 이탈(둘 다 전량). TDA는 청산에 미사용(자문 전용).
    #    근거(tda_exit_portfolio_backtest): TDA 청산은 방향게이트를 해도 time(미사용)보다 수익·MDD·Sharpe 모두 열위.
    #    리스크 관리는 포트폴리오 레벨 월 −3% 서킷브레이커가 담당.
    keep = []
    for p in positions:
        held = _trading_days_between(cal, p["entry_date"], day)
        full = "시간청산(40일)" if held >= hold_days else ("추세이탈(20주선)" if p["ticker"] in sells_set else None)
        if full:
            px = _price(sig, p["ticker"], p.get("last_price") or p["entry_price"])
            cash += _record_sell(p, p["shares"], px, held, full)
            continue
        keep.append(p)
    positions = keep

    # 2) 서킷브레이커 (당월 손익 ≤ −3%×투자금)
    eq_now, _ = _equity(cash, positions, sig)
    cur_pnl = eq_now - inv
    cb_month = state.get("cb_month")
    cb_base = state.get("cb_base_pnl", 0.0)
    mon = day[:7]
    if cb_month != mon:                 # 새 달 → 월초 손익 기준 갱신
        cb_month, cb_base = mon, cur_pnl
    tripped = (cur_pnl - cb_base) <= -CB_LIMIT * inv

    # 3) 매수 (미발동 & 슬롯 여유)
    slots = int(sig.get("exposure", {}).get("slots", 0))
    weight = float(sig.get("exposure", {}).get("weight", 0.0))
    if not tripped and slots > len(positions) and weight > 0:
        held_tk = {p["ticker"] for p in positions}
        for c in sig.get("buy_order", []):
            if len(positions) >= slots:
                break
            tk = c.get("ticker")
            if not tk or tk in held_tk:
                continue
            px = float(c.get("close") or 0)
            if px <= 0:
                continue
            sh = math.floor(weight * eq_now / px)
            spend = sh * px * (1 + cost / 2)
            if sh > 0 and cash >= spend:
                cash -= spend
                positions.append({"ticker": tk, "name": c.get("name", tk), "entry_date": day,
                                  "entry_price": px, "shares": sh, "last_price": px})
                held_tk.add(tk)

    # 4) 평가
    equity, hv = _equity(cash, positions, sig)
    new_state = {"investment": inv, "cash": round(cash, 2), "positions": positions,
                 "cb_month": cb_month, "cb_base_pnl": cb_base}
    result = {"date": day, "equity": round(equity), "cash": round(cash), "holdings_value": round(hv),
              "trades": trades, "tripped": tripped, "n_positions": len(positions)}
    return new_state, result


def new_state(investment):
    return {"investment": float(investment), "cash": float(investment), "positions": [],
            "cb_month": None, "cb_base_pnl": 0.0}
