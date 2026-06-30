"""추천 이력 영속화 — 매 배치에서 '20주선 눌림' 매수 추천 종목을 기록·갱신.
종목별: 최초 추천일·주차·사유·추천당시가·매수목표가(20주선 눌림대) + 매 회차 현재종가 갱신.
사용자 고민(언제·왜·얼마에 추천됐나) 해소 + 추천 이후 수익률 추적용. → state/reco_history.json
"""
from __future__ import annotations
import json, os
import pandas as pd

PATH = "state/reco_history.json"
PULLBACK_BAND = 1.05      # 20주선 눌림대 상단(진입 가능 구간 = w_ma20 ~ w_ma20×1.05)


def week_label(asof) -> str:
    """'2026년 6월 5주차' — 월 기준 주차(주봉 눌림 전략이라 주 단위로 묶음)."""
    d = pd.Timestamp(asof)
    wom = (d.day - 1) // 7 + 1
    return f"{d.year}년 {d.month}월 {wom}주차"


def update(candidates, stocks, close_map, asof, path=PATH, first_reco=None) -> dict:
    """candidates: 현재 매수 추천 종목코드 리스트(buy_order). stocks: 페이로드 stocks(close/wma20/rs/name…).
    close_map: {ticker: 최신종가}(전 상장 — 이력 종목 현재가 갱신용). asof: 'YYYY-MM-DD'.
    first_reco: {ticker:(reco_date, reco_close)} — bt_archive 백필(진짜 최근 추천 시작일·당시가). 없으면 asof.
    신규 추천은 추천일·주차·당시가·목표가 기록, 기존은 현재종가 갱신. 이력 dict 반환."""
    try:
        hist = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    except Exception:
        hist = {}
    today = str(asof)
    first_reco = first_reco or {}
    cand_set = set(candidates)
    for tk in candidates:
        s = stocks.get(tk)
        if not s:
            continue
        if tk not in hist:                      # ★최초 추천 — 시점·사유·당시가·목표가 고정★
            wma = s.get("wma20")
            rs = round(float(s.get("rs") or 0))
            bf = first_reco.get(tk)             # bt_archive 백필(진짜 추천 시작일·당시가) 우선
            rdate = (bf[0] if bf else today)
            rclose = (round(float(bf[1])) if (bf and bf[1]) else round(float(s["close"])))
            hist[tk] = {
                "ticker": tk, "name": s.get("name", tk), "market": s.get("market", ""), "sector": s.get("sector", ""),
                "reco_date": rdate, "week": week_label(rdate), "reco_close": rclose,
                "target_low": round(wma) if wma else None,
                "target_high": round(wma * PULLBACK_BAND) if wma else None,
                "rs": rs, "reason": f"F리더(RS {rs}) ∩ 20주선 눌림 진입대",
                "first_seen": today, "last_seen": today,
            }
        else:
            hist[tk]["last_seen"] = today        # 같은 종목 재추천 — 최초 기록 유지, last_seen만 갱신
    # 현재종가·활성여부 갱신(전 종목)
    for tk, h in hist.items():
        h["active"] = tk in cand_set
        cc = close_map.get(tk)
        if cc and cc > 0:
            h["cur_close"] = round(float(cc)); h["cur_date"] = today
        rc = h.get("reco_close")
        h["ret"] = (h["cur_close"] / rc - 1) if (rc and h.get("cur_close")) else None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(hist, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, path)
    return hist
