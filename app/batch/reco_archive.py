"""추천 이력 영속화 — 매 배치에서 '20주선 눌림' 매수 추천 종목을 기록·갱신.
종목별: 최초 추천일·주차·사유·추천당시가·매수목표가(20주선 눌림대) + 매 회차 현재종가 갱신.
사용자 고민(언제·왜·얼마에 추천됐나) 해소 + 추천 이후 수익률 추적용. → state/reco_history.json
"""
from __future__ import annotations
import bisect, json, os
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


def build_full_history(days, prices_df, weekly_ind, active_set, asof, names,
                       lookback_days=185, hold_days=40, gap_days=6) -> list:
    """bt_days(매 거래일 buy 신호) + bt_prices로 lookback 기간 '전체 추천 이력' 구성.
    종목별 '최근 추천 에피소드' 1건: 추천 시작일·주차·당시가·눌림대 목표가 +
    추천 후 hold_days 거래일 청산 실현수익률(미도래 시 현재가·보유중/추천중). reco_date 내림차순.
    days: {ds:{buy:[...]}} · prices_df: [date,ticker,close] · weekly_ind: 20주선 목표가용(없어도 됨)."""
    asof = str(asof)
    cal = sorted(d for d in days.keys() if d <= asof)
    if not cal:
        return []
    cal_idx = {d: i for i, d in enumerate(cal)}
    last_ds = cal[-1]
    since = str((pd.Timestamp(asof) - pd.Timedelta(days=lookback_days)).date())
    reco_dates = {}                              # 종목 → 기간 내 추천일들
    for ds in cal:
        if ds < since:
            continue
        for tk in (days[ds].get("buy") or []):
            reco_dates.setdefault(tk, []).append(ds)
    if not reco_dates:
        return []
    pr = {}
    psub = prices_df[prices_df["ticker"].isin(reco_dates.keys())].copy()
    psub["date"] = psub["date"].astype(str).str[:10]
    for tk, g in psub.sort_values("date").groupby("ticker"):
        pr[tk] = (list(g["date"]), list(g["close"].astype(float)))
    wk = {}
    if weekly_ind is not None and len(weekly_ind):
        wsub = weekly_ind[weekly_ind["ticker"].isin(reco_dates.keys())]
        for tk, g in wsub.sort_values("week_end").groupby("ticker"):
            wk[tk] = (list(g["week_end"].astype(str).str[:10]), list(g["w_ma20"]))

    def price_at(tk, ds):
        """반환 (close, quality). quality: exact=정확일치 · forward=다음 거래일가(갭) ·
        stale=요청일이 종목 데이터 종료 후(유니버스 이탈/상폐 가능 — 마지막 확인가, 수익 신뢰불가) · none=데이터 없음."""
        d = pr.get(tk)
        if not d:
            return None, "none"
        dates, closes = d
        if ds > dates[-1]:                       # 요청일이 저장 최종일보다 뒤 → 추적 끊김(조용한 미래가 대체 금지)
            return closes[-1], "stale"
        i = bisect.bisect_left(dates, ds)
        if i < len(dates):
            return closes[i], ("exact" if dates[i] == ds else "forward")
        return (closes[-1], "stale") if closes else (None, "none")

    def wma_at(tk, ds):
        d = wk.get(tk)
        if not d:
            return None
        dates, vals = d
        i = bisect.bisect_right(dates, ds) - 1
        if i < 0:
            return None
        v = vals[i]
        return float(v) if pd.notna(v) else None

    out = []
    for tk, dates in reco_dates.items():
        # 에피소드 분해 — 추천일 사이 간격>gap_days면 새 에피소드. 전체 목록 보존(회차별 수익 병렬 표시용).
        starts = [dates[0]]
        prev = dates[0]
        for d in dates[1:]:
            if cal_idx[d] - cal_idx[prev] > gap_days:
                starts.append(d)
            prev = d
        cur, curq = price_at(tk, last_ds)        # 현재가 + 품질(stale=유니버스 이탈로 추적 끊김)

        def _episode(start, is_latest):
            """'이 추천일에 샀다면' 시나리오 — 진입가·청산(40거래일 or 20주선 이탈)·실현/현재 수익."""
            rc, _ = price_at(tk, start)          # 진입일=매수신호일=유니버스일이라 항상 유효
            if not rc or rc <= 0:
                return None
            ii = cal_idx.get(start)
            ex_i = ex_r = None
            if ii is not None:
                capj = ii + hold_days
                for j in range(ii + 1, min(capj, len(cal) - 1) + 1):
                    if tk in (days[cal[j]].get("sells") or []):
                        ex_i, ex_r = j, "추세이탈"; break
                if ex_i is None and capj < len(cal):
                    ex_i, ex_r = capj, "40거래일"
            if ex_i is not None:
                ed, stt = cal[ex_i], "청산"
            else:
                ed, stt = last_ds, ("추천중" if (is_latest and tk in active_set) else "보유중")
            ec, ecq = price_at(tk, ed); wma = wma_at(tk, start)
            stale = (ecq == "stale") or (curq == "stale")   # 청산가/현재가가 데이터 종료 후 → 수익 신뢰불가(이탈/상폐 가능)
            if ex_i is None and stale:           # 미청산인데 추적 끊김 → '보유중'이 아니라 데이터종료
                stt = "데이터종료"
            return {"reco_date": start, "week": week_label(start), "reco_close": round(rc),
                    "target_low": round(wma) if wma else None,
                    "target_high": round(wma * PULLBACK_BAND) if wma else None,
                    "exit_date": ed, "exit_close": round(ec) if ec else None,
                    "exit_reason": (ex_r if ex_r else ("데이터종료(이탈/상폐 가능)" if stale else None)),
                    "ret_hold": (ec / rc - 1) if (ec and rc) else None,     # 그 진입의 실현(청산) 또는 현재 수익
                    "ret_now": (cur / rc - 1) if (cur and rc) else None,    # 현재가 기준 수익(보유 지속 가정)
                    "stale": stale, "status": stt}

        episodes = [e for e in (_episode(s, s == starts[-1]) for s in starts) if e]
        if not episodes:
            continue
        first_ep, latest_ep = episodes[0], episodes[-1]
        n_ep = len(episodes)
        # 첫 추천 이후 '20주선 이탈(매도신호)' 한 번이라도 있었나 → 없으면 첫 추천분 아직 연속보유
        fi = cal_idx.get(first_ep["reco_date"])
        ever_sold = any(tk in (days[cal[j]].get("sells") or []) for j in range(fi + 1, len(cal))) if fi is not None else False
        data_stale = (curq == "stale")                       # 현재가 추적 끊김(유니버스 이탈/상폐 가능)
        # 연속보유 = 매도신호 없음 & 40거래일 미도래 & 추적 데이터 살아있음(stale면 보유 주장 불가)
        held_continuous = (not ever_sold) and (not data_stale) and ((cal_idx[last_ds] - fi) < hold_days if fi is not None else False)
        # 대표(카드 헤더)는 연속보유면 첫 추천, 아니면 최근 에피소드
        primary = first_ep if held_continuous else latest_ep
        # 대표 상태: 추적 끊김>현재 추천(active)>대표 에피소드 순
        disp_status = "데이터종료" if data_stale else ("추천중" if tk in active_set else primary["status"])
        out.append({
            "ticker": tk, "name": names.get(tk, tk),
            "reco_date": primary["reco_date"], "week": primary["week"], "reco_close": primary["reco_close"],
            "target_low": primary["target_low"], "target_high": primary["target_high"],
            "exit_date": primary["exit_date"], "exit_close": primary["exit_close"], "exit_reason": primary["exit_reason"],
            "ret_hold": primary["ret_hold"], "cur_close": round(cur) if cur else None,
            "ret": primary["ret_now"],
            "status": disp_status,
            "active": tk in active_set, "reason": "F리더 ∩ 20주선 눌림",
            "data_stale": data_stale,                        # 유니버스 이탈/상폐로 현재가 추적 끊김(수익 신뢰불가)
            # ── 회차별 수익 병렬 표시(재추천) + 매도신호/연속보유 ──
            "episodes": episodes,                            # 각 추천 회차: 진입일·진입가·청산·실현/현재 수익
            "n_episodes": n_ep,
            "first_reco_date": first_ep["reco_date"], "first_reco_week": first_ep["week"],
            "first_reco_close": first_ep["reco_close"], "ret_since_first": first_ep["ret_now"],
            "ever_sold": ever_sold,                          # 첫 추천 후 20주선 이탈(매도신호) 발생 여부
            "held_continuous": held_continuous,              # 매도신호 없이 아직 보유중(40거래일 시간청산 전)
            "held_days": (cal_idx[last_ds] - cal_idx.get(primary["reco_date"], cal_idx[last_ds])),  # 대표 진입 후 경과 거래일(40일차 시간청산)
            "hold_days": hold_days,                          # 시간청산 기준일(=40)
        })
    # 추천일 내림차순 + 동률 tie-breaker(수익 desc, 종목코드) — 재현성 확보(비결정 정렬 방지)
    out.sort(key=lambda e: (e["reco_date"], e["ret"] if e["ret"] is not None else -9, e["ticker"]), reverse=True)
    return out
