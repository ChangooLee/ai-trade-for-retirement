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
MAINT_MARGIN = 0.30      # 유지증거금: 자기자본/보유평가액 < 이 값 → 마진콜(강제청산)
MARGIN_RATE = 0.07       # 신용융자 연이자(차입잔액 일할 부과)


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
    max_lev = float(sig.get("exposure", {}).get("max_lev", 1.0))    # 그날 목표노출(=매수여력 배수). ≤1이면 무차입(기존동작)
    mrate = float(sig.get("margin_rate", MARGIN_RATE))
    maint = float(sig.get("maint_margin", MAINT_MARGIN))
    margin_interest = 0.0
    if cash < 0 and mrate > 0:                       # 전일 차입잔액에 하루치 이자
        margin_interest = -cash * (mrate / 252.0)
        cash -= margin_interest
    cal = sig.get("calendar", [])
    hold_days = int(sig.get("hold_days", 40))
    sells_set = set(sig.get("sell_tickers", []))     # 추세이탈(20주선) — 전량청산
    cut_days = int(sig.get("early_cut_days", 0))     # 정체컷: K거래일 이상 보유 & 수익률≤cut_ret면 조기청산(0=끔)
    cut_ret = float(sig.get("early_cut_ret", 0.0))   # '가망없음' 임계(예: 0.01=+1% 이하면 데드머니로 보고 청산)
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
        if not full and cut_days and held >= cut_days:   # 정체컷: 충분히 보유했는데 진척 없으면 데드머니 청산
            cur = _price(sig, p["ticker"], p.get("last_price") or p["entry_price"])
            if p["entry_price"] and (cur / p["entry_price"] - 1) <= cut_ret:
                full = "정체컷"
        if full:
            px = _price(sig, p["ticker"], p.get("last_price") or p["entry_price"])
            cash += _record_sell(p, p["shares"], px, held, full)
            continue
        keep.append(p)
    positions = keep

    # 2) 서킷브레이커 (당월 손익 ≤ −limit×투자금). 모드: block=신규매수만 중단 / liq=전량청산 후 그달 중단.
    eq_now, _ = _equity(cash, positions, sig)
    cur_pnl = eq_now - inv
    cb_limit = float(state.get("cb_limit", CB_LIMIT))      # 사용자 조정 가능(공격성). 0 또는 None이면 끔.
    cb_mode = state.get("cb_mode", "block")                # 기본 block. ★양분할 검증(2026-06): −3% liq는 수익 최악·MDD 이점無
    #   (전·후반 모두 block≫liq); liq 쓰려면 −5% 권장(두 반기 MDD 최저). 과거 'liq 우위' 주석은 기각.★
    cb_month = state.get("cb_month")
    cb_base = state.get("cb_base_pnl", 0.0)
    mon = day[:7]
    if cb_month != mon:                 # 새 달 → 월초 손익 기준 갱신
        cb_month, cb_base = mon, cur_pnl
    tripped = (cb_limit > 0) and ((cur_pnl - cb_base) <= -cb_limit * inv)
    cb_liq_month = state.get("cb_liq_month")              # liqsoft가 '그달 이미 청산했는지' 추적
    just_liq = False
    # liq=전량청산 후 그달 내내 현금 / liqsoft=그달 1회만 청산하고 다음날부터 정상 재진입(덜 거침)
    if tripped and cb_mode in ("liq", "liqsoft") and positions and (cb_mode == "liq" or cb_liq_month != mon):
        for p in list(positions):
            px = _price(sig, p["ticker"], p.get("last_price") or p["entry_price"])
            held = _trading_days_between(cal, p["entry_date"], day)
            cash += _record_sell(p, p["shares"], px, held, "브레이커청산")
        positions = []; just_liq = True
        if cb_mode == "liqsoft":
            cb_liq_month = mon
    # 매수 차단: block/liq=발동 동안 차단 / liqsoft=청산한 그날만 차단(이후 정상 재진입) / none=차단 없음
    blocked = tripped if cb_mode in ("block", "liq") else (just_liq if cb_mode == "liqsoft" else False)

    # 2.5) 마진콜 — 차입(현금<0) 중 자기자본/보유평가 < 유지증거금이면 전량 강제청산
    margin_called = False
    eq_mc, hv_mc = _equity(cash, positions, sig)
    if cash < 0 and hv_mc > 0 and eq_mc < maint * hv_mc:
        for p in list(positions):
            px = _price(sig, p["ticker"], p.get("last_price") or p["entry_price"])
            held = _trading_days_between(cal, p["entry_date"], day)
            cash += _record_sell(p, p["shares"], px, held, "마진콜")
        positions = []; margin_called = True; blocked = True

    # 3) 매수 (미차단 & 슬롯 여유). 차입한도 = (max_lev−1)×자기자본까지 현금 마이너스 허용
    eq_now, _ = _equity(cash, positions, sig)
    borrow_floor = -max(0.0, max_lev - 1.0) * eq_now      # max_lev≤1 → 0 → 기존 무차입 동작과 동일
    slots = int(sig.get("exposure", {}).get("slots", 0))
    weight = float(sig.get("exposure", {}).get("weight", 0.0))
    if not blocked and slots > len(positions) and weight > 0:
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
            if sh > 0 and (cash - spend) >= borrow_floor - 1e-6:
                cash -= spend
                positions.append({"ticker": tk, "name": c.get("name", tk), "entry_date": day,
                                  "entry_price": px, "shares": sh, "last_price": px})
                held_tk.add(tk)

    # 4) 평가
    equity, hv = _equity(cash, positions, sig)
    new_state = {"investment": inv, "cash": round(cash, 2), "positions": positions,
                 "cb_month": cb_month, "cb_base_pnl": cb_base, "cb_limit": cb_limit, "cb_mode": cb_mode,
                 "cb_liq_month": cb_liq_month}
    borrowed = max(0.0, -cash)
    result = {"date": day, "equity": round(equity), "cash": round(cash), "holdings_value": round(hv),
              "trades": trades, "tripped": tripped, "n_positions": len(positions),
              "borrowed": round(borrowed), "leverage": round(hv / equity, 3) if equity > 0 else 0,
              "margin_interest": round(margin_interest), "margin_called": margin_called}
    return new_state, result


def new_state(investment, cb_limit=CB_LIMIT, cb_mode="block"):
    return {"investment": float(investment), "cash": float(investment), "positions": [],
            "cb_month": None, "cb_base_pnl": 0.0, "cb_limit": float(cb_limit), "cb_mode": cb_mode,
            "cb_liq_month": None}


_CORE_NAME = {"KOSPI": "KODEX 200(코어)", "KOSDAQ": "KODEX 코스닥150(코어)"}


def execute_day_coresat(state, day, sig):
    """코어-위성 하루 집행 — 코어=추세 위 지수 로테이션(core_weight), 위성=active 종목((1-core_weight)).

    코어 포지션은 positions 내 특수항목 {core:True, index, shares(=지수단위), entry_price(=지수레벨)}로 보관.
    · 코어: 추세 위 지수(sig.core_alloc.lead)로 로테이션. 리밸=코어없음/리드전환/±15%드리프트일 때만(저churn).
      두 지수 모두 추세 아래(lead=None)면 코어 청산→현금(방어). 지수 레벨을 ETF 프록시가로 사용(무배당·수수료 근사).
    · 위성: 기존 active 청산(40일/20주선/정체컷) + 매수(D4 노출 m 내). 슬리브 비중=(1-core_weight).
    · 시장 리스크 관리는 코어 로테이션이 담당 → 위성에 월 서킷브레이커 미적용(단순·견고).
    """
    inv = float(state["investment"]); cash = float(state["cash"])
    positions = [dict(p) for p in state.get("positions", [])]
    cost = float(sig.get("cost", 0.0035))
    cw = min(max(float(state.get("core_weight", 0.7)), 0.0), 1.0)
    ca = sig.get("core_alloc", {}) or {}
    lead = ca.get("lead")
    idx_px = {"KOSPI": ca.get("kospi"), "KOSDAQ": ca.get("kosdaq")}
    cal = sig.get("calendar", []); hold_days = int(sig.get("hold_days", 40))
    sells_set = set(sig.get("sell_tickers", []))
    cut_days = int(sig.get("early_cut_days", 0)); cut_ret = float(sig.get("early_cut_ret", 0.0))
    trades = []

    core = next((p for p in positions if p.get("core")), None)
    sats = [p for p in positions if not p.get("core")]

    for p in sats:                                   # 위성 최신가=종목 종가
        px = sig.get("prices", {}).get(p["ticker"])
        if px and px > 0:
            p["last_price"] = float(px)
    if core:                                         # 코어 최신가=지수 레벨
        cpx = idx_px.get(core.get("index"))
        if cpx and cpx > 0:
            core["last_price"] = float(cpx)

    def _cval(c):
        if not c:
            return 0.0
        px = idx_px.get(c.get("index")) or c.get("last_price") or c.get("entry_price")
        return c["shares"] * float(px or 0)

    def _sval(ps):
        return sum(p["shares"] * _price(sig, p["ticker"], p.get("last_price") or p["entry_price"]) for p in ps)

    def _record_sell(p, sh, px, held, reason):
        proceeds = sh * px * (1 - cost / 2)
        buy_cost = p["entry_price"] * sh * (1 + cost / 2)
        trades.append({"ticker": p["ticker"], "name": p.get("name", p["ticker"]),
                       "entry_date": p["entry_date"], "entry_price": p["entry_price"],
                       "exit_date": day, "exit_price": px, "shares": sh,
                       "pnl": round(proceeds - buy_cost), "ret": (px / p["entry_price"] - 1) if p["entry_price"] else 0.0,
                       "days": held, "reason": reason})
        return proceeds

    # 1) 위성 청산 — 40거래일 / 20주선 이탈 / 정체컷(코어는 제외)
    keep = []
    for p in sats:
        held = _trading_days_between(cal, p["entry_date"], day)
        full = "시간청산(40일)" if held >= hold_days else ("추세이탈(20주선)" if p["ticker"] in sells_set else None)
        if not full and cut_days and held >= cut_days:
            cur = _price(sig, p["ticker"], p.get("last_price") or p["entry_price"])
            if p["entry_price"] and (cur / p["entry_price"] - 1) <= cut_ret:
                full = "정체컷"
        if full:
            px = _price(sig, p["ticker"], p.get("last_price") or p["entry_price"])
            cash += _record_sell(p, p["shares"], px, held, full)
            continue
        keep.append(p)
    sats = keep

    equity = cash + _cval(core) + _sval(sats)         # 마크투마켓 에쿼티(사이징 기준)
    target_core = cw * equity

    # 2) 코어 리밸런스 — 로테이션. 저churn: 코어없음/리드전환/±15%드리프트만.
    if lead is None:                                  # 두 지수 추세 아래 → 코어 청산(현금 방어)
        if core:
            px = float(idx_px.get(core["index"]) or core.get("last_price") or core["entry_price"])
            held = _trading_days_between(cal, core["entry_date"], day)
            cash += _record_sell(core, core["shares"], px, held, "코어→현금(추세이탈)")
            core = None
    else:
        cur_core = _cval(core)
        drift = abs(cur_core / target_core - 1) if target_core > 0 else (1.0 if core else 0.0)
        need = (core is None) or (core.get("index") != lead) or (drift > 0.15)
        if need:
            lpx = idx_px.get(lead)
            if lpx and lpx > 0:
                if core:                              # 기존 코어 청산(전환/리밸)
                    px = float(idx_px.get(core["index"]) or core.get("last_price") or core["entry_price"])
                    held = _trading_days_between(cal, core["entry_date"], day)
                    cash += _record_sell(core, core["shares"], px, held,
                                         "코어 전환" if core["index"] != lead else "코어 리밸런스")
                    core = None
                spend = min(target_core, max(0.0, cash))     # 코어 우선, 현금 한도(무차입)
                units = spend / (lpx * (1 + cost / 2))
                if units > 0:
                    cash -= units * lpx * (1 + cost / 2)
                    core = {"ticker": f"_CORE_{lead}", "core": True, "index": lead,
                            "name": _CORE_NAME.get(lead, f"{lead} 코어"), "entry_date": day,
                            "entry_price": lpx, "shares": units, "last_price": lpx}

    # 3) 위성 매수 — 슬리브 비중=(1-cw). per-position=weight×(1-cw)×equity, 현금 한도.
    slots = int(sig.get("exposure", {}).get("slots", 0))
    weight = float(sig.get("exposure", {}).get("weight", 0.0)) * (1.0 - cw)
    if slots > len(sats) and weight > 0:
        held_tk = {p["ticker"] for p in sats}
        for c in sig.get("buy_order", []):
            if len(sats) >= slots:
                break
            tk = c.get("ticker")
            if not tk or tk in held_tk:
                continue
            px = float(c.get("close") or 0)
            if px <= 0:
                continue
            sh = math.floor(weight * equity / px)
            spend = sh * px * (1 + cost / 2)
            if sh > 0 and cash >= spend:
                cash -= spend
                sats.append({"ticker": tk, "name": c.get("name", tk), "entry_date": day,
                             "entry_price": px, "shares": sh, "last_price": px})
                held_tk.add(tk)

    # 4) 평가
    positions = ([core] if core else []) + sats
    cv, sv = _cval(core), _sval(sats)
    hv = cv + sv
    equity_final = cash + hv
    new_state = {"investment": inv, "cash": round(cash, 2), "positions": positions,
                 "core_weight": cw, "cb_month": state.get("cb_month"),
                 "cb_base_pnl": state.get("cb_base_pnl", 0.0),
                 "cb_limit": state.get("cb_limit", 0.03), "cb_mode": state.get("cb_mode", "block")}
    result = {"date": day, "equity": round(equity_final), "cash": round(cash), "holdings_value": round(hv),
              "trades": trades, "tripped": False, "n_positions": len(sats),
              "core_index": (core.get("index") if core else None),
              "core_value": round(cv), "sat_value": round(sv),
              "core_pct": round(cv / equity_final, 3) if equity_final > 0 else 0}
    return new_state, result
