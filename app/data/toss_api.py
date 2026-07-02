"""토스증권 OpenAPI 클라이언트 — client_credentials 토큰(24h 캐시) + 시세/계좌/주문.

보안: api_key·secret·access_token은 절대 로깅/출력하지 않는다. 주문(POST create/modify/cancel)은
상위(안전장치·소유자 인증·한도·킬스위치)에서만 호출한다. 읽기(GET)는 안전.

symbol: KRX=6자리 숫자(005930), US=영문 티커(AAPL). 콤마로 다중(최대 200).
주문조회 status: OPEN|CLOSED. 주문유형: LIMIT|MARKET. 방향: BUY|SELL. TIF: DAY|CLS.
"""
from __future__ import annotations
import json, threading, time, urllib.error, urllib.parse, urllib.request

BASE = "https://openapi.tossinvest.com"
_tok_cache: dict = {}          # api_key -> (access_token, expiry_epoch)
_lock = threading.Lock()


class TossError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        # 본문에 키/토큰이 들어갈 일은 없으나, 방어적으로 400자 제한
        super().__init__(f"HTTP {status}: {str(body)[:400]}")


def _http(method, path, headers=None, data=None, timeout=20, _tries=4):
    for attempt in range(_tries):
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _tries - 1:      # 레이트리밋 → 백오프 후 재시도(최대 _tries)
                ra = e.headers.get("Retry-After")
                try:
                    time.sleep(float(ra) if ra else (0.8 + 0.7 * attempt))
                except (TypeError, ValueError):
                    time.sleep(0.8 + 0.7 * attempt)
                continue
            try:
                eb = json.loads(e.read().decode() or "{}")
            except Exception:
                eb = {}
            raise TossError(e.code, eb)


def get_token(api_key, secret):
    """client_credentials 토큰 발급/캐시(24h, 만료 60s 전 갱신)."""
    now = time.time()
    with _lock:
        c = _tok_cache.get(api_key)
        if c and c[1] - now > 60:
            return c[0]
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": api_key, "client_secret": secret}).encode()
    st, js, _ = _http("POST", "/oauth2/token",
                      {"Content-Type": "application/x-www-form-urlencoded"}, body)
    at = js.get("access_token")
    if not at:
        raise TossError(st, {"error": "no access_token"})
    with _lock:
        _tok_cache[api_key] = (at, now + float(js.get("expires_in", 3600)))
    return at


def _auth(token, account=None):
    h = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if account is not None:
        h["X-Tossinvest-Account"] = str(account)
    return h


def _q(d):
    return "?" + urllib.parse.urlencode({k: v for k, v in d.items() if v is not None}) if d else ""


# ── 계좌/자산 (읽기 전용) ──
def get_accounts(api_key, secret):
    return _http("GET", "/api/v1/accounts", _auth(get_token(api_key, secret)))[1]


def get_holdings(api_key, secret, account, symbol=None):
    return _http("GET", "/api/v1/holdings" + _q({"symbol": symbol}),
                 _auth(get_token(api_key, secret), account))[1]


def get_orders(api_key, secret, account, status="OPEN", symbol=None, limit=None, cursor=None):
    return _http("GET", "/api/v1/orders" + _q({"status": status, "symbol": symbol, "limit": limit, "cursor": cursor}),
                 _auth(get_token(api_key, secret), account))[1]


def get_order(api_key, secret, account, order_id):
    return _http("GET", f"/api/v1/orders/{order_id}", _auth(get_token(api_key, secret), account))[1]


def get_orders_paged(api_key, secret, account, status="CLOSED", pages=3, per=50):
    """주문 이력 페이지네이션(cursor) — 인사이트용 다건 수집."""
    out, cursor = [], None
    for _ in range(max(1, pages)):
        r = (get_orders(api_key, secret, account, status=status, limit=per, cursor=cursor) or {}).get("result") or {}
        out.extend(r.get("orders") or [])
        cursor = r.get("nextCursor")
        if not cursor or not r.get("hasNext"):
            break
    return out


def get_buying_power(api_key, secret, account, currency="KRW"):
    """현금 매수여력 {currency, cashBuyingPower}. currency: KRW|USD."""
    return _http("GET", "/api/v1/buying-power" + _q({"currency": currency}), _auth(get_token(api_key, secret), account))[1]


def get_sellable(api_key, secret, account, symbol):
    """매도 가능 수량."""
    return _http("GET", "/api/v1/sellable-quantity" + _q({"symbol": symbol}), _auth(get_token(api_key, secret), account))[1]


def get_exchange_rate(api_key, secret, base_currency="USD", quote_currency="KRW"):
    """환율 조회 — base/quote 통화쌍(예: USD→KRW). 응답 {rate, midRate, ...}."""
    return _http("GET", "/api/v1/exchange-rate" + _q({"baseCurrency": base_currency, "quoteCurrency": quote_currency}),
                 _auth(get_token(api_key, secret)))[1]


def get_price_limits(api_key, secret, symbol):
    """상/하한가 조회."""
    return _http("GET", "/api/v1/price-limits" + _q({"symbol": symbol}), _auth(get_token(api_key, secret)))[1]


def get_warnings(api_key, secret, symbol):
    """매수 유의사항 조회."""
    return _http("GET", f"/api/v1/stocks/{symbol}/warnings", _auth(get_token(api_key, secret)))[1]


def get_market_calendar(api_key, secret, market="KR"):
    """장 운영 정보(개장 여부·시간). market: KR|US."""
    mk = "US" if str(market).upper() == "US" else "KR"
    return _http("GET", f"/api/v1/market-calendar/{mk}", _auth(get_token(api_key, secret)))[1]


# ── 시세 (읽기 전용, 계좌 불필요) ──
def get_prices(api_key, secret, symbols):
    return _http("GET", "/api/v1/prices" + _q({"symbols": symbols}), _auth(get_token(api_key, secret)))[1]


def get_candles(api_key, secret, symbol, interval="1d", count=None, before=None, adjusted=None):
    return _http("GET", "/api/v1/candles" + _q({"symbol": symbol, "interval": interval, "count": count,
                 "before": before, "adjusted": None if adjusted is None else str(adjusted).lower()}),
                 _auth(get_token(api_key, secret)))[1]


def get_orderbook(api_key, secret, symbol):
    return _http("GET", "/api/v1/orderbook" + _q({"symbol": symbol}), _auth(get_token(api_key, secret)))[1]


def get_stocks(api_key, secret, symbols):
    return _http("GET", "/api/v1/stocks" + _q({"symbols": symbols}), _auth(get_token(api_key, secret)))[1]


# ── 주문 (POST) — Stage2/3 전용. 상위 안전장치(소유자 인증·한도·킬스위치)에서만 호출 ──
def create_order(api_key, secret, account, symbol, side, order_type,
                 quantity=None, price=None, order_amount=None, time_in_force="DAY",
                 client_order_id=None, confirm_high_value=False):
    """주문 생성. 수량기반(KRX+US) 또는 금액기반(order_amount, US MARKET 전용).
    side: BUY|SELL, order_type: LIMIT|MARKET. price는 LIMIT일 때만."""
    body = {"symbol": symbol, "side": side, "orderType": order_type,
            "confirmHighValueOrder": bool(confirm_high_value)}
    if order_amount is not None:
        body["orderAmount"] = str(order_amount)      # 금액기반(US MARKET)
    else:
        body["quantity"] = str(quantity)
        body["timeInForce"] = time_in_force
        if order_type == "LIMIT" and price is not None:
            body["price"] = str(price)
    if client_order_id:
        body["clientOrderId"] = client_order_id
    h = _auth(get_token(api_key, secret), account)
    h["Content-Type"] = "application/json"
    return _http("POST", "/api/v1/orders", h, json.dumps(body).encode())[1]


def cancel_order(api_key, secret, account, order_id):
    h = _auth(get_token(api_key, secret), account)
    h["Content-Type"] = "application/json"
    return _http("POST", f"/api/v1/orders/{order_id}/cancel", h, b"{}")[1]


def modify_order(api_key, secret, account, order_id, order_type, quantity=None, price=None, confirm_high_value=False):
    body = {"orderType": order_type, "confirmHighValueOrder": bool(confirm_high_value)}
    if quantity is not None:
        body["quantity"] = str(quantity)
    if price is not None:
        body["price"] = str(price)
    h = _auth(get_token(api_key, secret), account)
    h["Content-Type"] = "application/json"
    return _http("POST", f"/api/v1/orders/{order_id}/modify", h, json.dumps(body).encode())[1]
