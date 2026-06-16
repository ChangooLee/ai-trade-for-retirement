"""한국투자증권(KIS) OpenAPI — ★읽기전용 실시간 시세★. 주문/체결 기능 없음(AGENTS.md §2: 실거래 코드 금지).

- 인증: oauth2/tokenP (grant_type=client_credentials, appkey/appsecret). 토큰 24h 유효 →
        state/kis_token.json 캐시(발급 rate-limit 1회/분 회피).
- 시세: /uapi/domestic-stock/v1/quotations/inquire-price (현재가). tr_id=FHKST01010100.
- 환경변수: KIS_API_KEY, KIS_API_SECRET (.env, gitignored). KIS_BASE 기본=실전 도메인.
- ★주문 엔드포인트(/order 등)는 의도적으로 구현하지 않음.★
"""
from __future__ import annotations
import json, os, time
import requests

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASE = os.environ.get("KIS_BASE", "https://openapi.koreainvestment.com:9443")
TOKEN_PATH = os.path.join(_REPO, "state", "kis_token.json")


def _load_env():
    """os.environ에 없으면 repo .env에서 KIS 키 로드(값은 반환만, 로깅 안 함)."""
    k, s = os.environ.get("KIS_API_KEY"), os.environ.get("KIS_API_SECRET")
    if k and s:
        return k, s
    envp = os.path.join(_REPO, ".env")
    if os.path.exists(envp):
        for line in open(envp, encoding="utf-8"):
            line = line.strip()
            if line.startswith("KIS_API_KEY="): k = line.split("=", 1)[1]
            elif line.startswith("KIS_API_SECRET="): s = line.split("=", 1)[1]
    if not (k and s):
        raise RuntimeError("KIS_API_KEY/SECRET 미설정(.env 확인)")
    return k, s


def get_token(force=False):
    """유효 토큰 반환. 캐시(state/kis_token.json) 우선, 만료 임박 시 재발급."""
    if not force and os.path.exists(TOKEN_PATH):
        try:
            c = json.load(open(TOKEN_PATH, encoding="utf-8"))
            if c.get("expires_at", 0) - time.time() > 600:   # 만료 10분 전까지 재사용
                return c["access_token"]
        except Exception:
            pass
    key, sec = _load_env()
    r = requests.post(f"{BASE}/oauth2/tokenP",
                      json={"grant_type": "client_credentials", "appkey": key, "appsecret": sec},
                      timeout=15)
    if r.status_code != 200:
        # 시크릿 노출 방지: 응답 본문만(요청 본문 X), 메시지 위주
        raise RuntimeError(f"토큰 발급 실패 {r.status_code}: {r.json().get('error_description', r.text[:200]) if r.headers.get('content-type','').startswith('application/json') else r.text[:200]}")
    d = r.json()
    tok = d["access_token"]
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    tmp = TOKEN_PATH + ".tmp"
    json.dump({"access_token": tok, "expires_at": time.time() + int(d.get("expires_in", 86400))},
              open(tmp, "w"))
    os.replace(tmp, TOKEN_PATH)
    return tok


def get_price(ticker):
    """현재가(원) 등 핵심 시세 dict. 읽기전용."""
    key, sec = _load_env()
    r = requests.get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                     headers={"authorization": f"Bearer {get_token()}", "appkey": key, "appsecret": sec,
                              "tr_id": "FHKST01010100", "custtype": "P"},
                     params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"시세 조회 실패 {r.status_code}: {r.text[:200]}")
    o = r.json().get("output", {})
    if not o:
        raise RuntimeError(f"시세 없음: {r.json().get('msg1', '')[:120]}")
    return {"ticker": ticker, "price": float(o.get("stck_prpr") or 0),       # 현재가
            "change_pct": float(o.get("prdy_ctrt") or 0),                     # 전일대비율
            "open": float(o.get("stck_oprc") or 0), "high": float(o.get("stck_hgpr") or 0),
            "low": float(o.get("stck_lwpr") or 0), "volume": float(o.get("acml_vol") or 0),
            "name": o.get("hts_kor_isnm", "")}


def get_prices(tickers):
    """여러 종목 현재가(순차 — KIS REST는 초당 호출 제한). {ticker: dict}."""
    out = {}
    for tk in tickers:
        try:
            out[tk] = get_price(tk); time.sleep(0.06)   # ~20req/s 미만
        except Exception as e:
            out[tk] = {"ticker": tk, "error": str(e)[:120]}
    return out


def get_minute_bars(ticker, date, market="J"):
    """date(YYYYMMDD)의 1분봉 전체(≈09:00~15:30) 페이지네이션 수집 → 시간오름차순 list[dict].
    FHKST03010230: 호출당 120건, FID_INPUT_HOUR_1 기준 과거방향. 보관 약 1년."""
    key, sec = _load_env(); tok = get_token()
    bars = {}; hour = "153000"
    for _ in range(8):                                   # 120*8>391, 안전 상한
        r = requests.get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
                         headers={"authorization": f"Bearer {tok}", "appkey": key, "appsecret": sec,
                                  "tr_id": "FHKST03010230", "custtype": "P"},
                         params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": ticker,
                                 "FID_INPUT_DATE_1": date, "FID_INPUT_HOUR_1": hour,
                                 "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": "N"}, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"분봉 조회 실패 {r.status_code}: {r.text[:160]}")
        o2 = r.json().get("output2") or []
        before = len(bars)
        for b in o2:
            t = b.get("stck_cntg_hour")
            if not t or t in bars:
                continue
            try:
                bars[t] = {"date": date, "time": t, "open": float(b["stck_oprc"]), "high": float(b["stck_hgpr"]),
                           "low": float(b["stck_lwpr"]), "close": float(b["stck_prpr"]), "vol": float(b.get("cntg_vol") or 0)}
            except (KeyError, ValueError, TypeError):
                pass
        if not bars or len(bars) == before:
            break
        earliest = min(bars)
        if earliest <= "090000":
            break
        m = int(earliest[:2]) * 60 + int(earliest[2:4]) - 1     # 다음 페이지: 최소시각 −1분
        if m < 540:
            break
        hour = f"{m // 60:02d}{m % 60:02d}00"; time.sleep(0.06)
    return [bars[t] for t in sorted(bars)]


def get_investor_flow(ticker, market="J"):
    """종목별 투자자 순매수 EOD 30일 트레일링(FHKST01010900). 외국인/기관/개인 순매수 거래대금(원)+종가.
    빈값(당일 미확정) 행은 제외. 시간오름차순 list[dict]."""
    key, sec = _load_env(); tok = get_token()
    r = requests.get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-investor",
                     headers={"authorization": f"Bearer {tok}", "appkey": key, "appsecret": sec,
                              "tr_id": "FHKST01010900", "custtype": "P"},
                     params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": ticker}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"수급 조회 실패 {r.status_code}: {r.text[:160]}")

    def _f(b, k):
        v = str(b.get(k, "")).strip().replace(",", "")
        try:
            return float(v) if v not in ("", "-") else None
        except ValueError:
            return None
    out = []
    for b in (r.json().get("output") or []):
        d = b.get("stck_bsop_date"); fr = _f(b, "frgn_ntby_tr_pbmn")
        if d and fr is not None:                      # 빈(당일 미확정) 행 제외
            out.append({"date": d, "close": _f(b, "stck_clpr"), "frgn_ntby": fr,
                        "orgn_ntby": _f(b, "orgn_ntby_tr_pbmn"), "prsn_ntby": _f(b, "prsn_ntby_tr_pbmn")})
    return list(reversed(out))                         # 오래된→최신


if __name__ == "__main__":   # 검증: 삼성전자 현재가 (시크릿 미출력)
    import sys
    tks = sys.argv[1:] or ["005930"]
    print(f"KIS 도메인: {BASE}")
    for tk, v in get_prices(tks).items():
        if "error" in v: print(f"  {tk}: 오류 {v['error']}")
        else: print(f"  {tk} {v['name']}: 현재가 {v['price']:,.0f}원 ({v['change_pct']:+.2f}%) · 거래량 {v['volume']:,.0f}")
