"""LEADERS DESK 동기화 API — 구글 ID 토큰 검증 후 사용자별 데이터 저장/조회 (자체 호스팅).

- 인증: 클라이언트가 구글 로그인으로 받은 ID 토큰을 Authorization: Bearer 로 전달.
        google-auth로 서명·만료·issuer·audience(=GOOGLE_CLIENT_ID) 로컬 검증 → 사용자 고유 sub 추출.
- 저장: state/userdata/{sub}.json (gitignored). 본인 sub로만 read/write — 교차 접근 불가.
- 배포: nginx `/trading/api/` → 127.0.0.1:SYNC_PORT, systemd로 상시 구동.
- 의존성: google-auth (requirements). 웹 프레임워크 없이 stdlib http.server.

환경변수: GOOGLE_CLIENT_ID(필수, 공개값) · SYNC_PORT(기본 8799)
"""
from __future__ import annotations
import json, os, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _load_env_file():
    """서비스가 systemd EnvironmentFile 없이도 .env(gitignored)의 키를 로드(기존 환경 우선)."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                if k and k not in os.environ:
                    os.environ[k] = v.strip()
    except FileNotFoundError:
        pass


_load_env_file()

# SSE 동시 스트림 상한(ThreadingHTTPServer는 스트림당 스레드 1개 점유) + 활성 카운터
MAX_STREAMS = 64
ACTIVE_STREAMS = 0
ACTIVE_LOCK = threading.Lock()
_DART_CACHE = {}          # {ticker: (ts, profile)} — DART 프로파일 6h 캐시(느린 다중 조회 회피)
_DART_TTL = 6 * 3600

PORT = int(os.environ.get("SYNC_PORT", "8799"))
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "..", "state", "userdata")
os.makedirs(DATA_DIR, exist_ok=True)
MAX_BODY = 2_000_000   # 2MB 상한

try:
    from google.oauth2 import id_token as _gid
    from google.auth.transport import requests as _greq
    _REQ = _greq.Request()
except Exception:                       # google-auth 미설치 시 — 검증 불가(전부 401)
    _gid = None; _REQ = None


def verify_token(tok):
    """원시 ID 토큰 검증 → {"sub","email"} 또는 None. (헤더/쿼리 공용)"""
    if _gid is None or not CLIENT_ID or not tok:
        return None
    try:
        info = _gid.verify_oauth2_token(tok, _REQ, CLIENT_ID)
        if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            return None
        sub = str(info.get("sub", ""))
        if not sub.isdigit():
            return None
        return {"sub": sub, "email": info.get("email", "")}
    except Exception:
        return None


def verify(headers):
    """Authorization: Bearer 헤더 검증 → 사용자 sub 또는 None."""
    auth = headers.get("Authorization", "")
    tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
    return verify_token(tok)


def path_for(sub):
    return os.path.join(DATA_DIR, f"{sub}.json")     # sub는 숫자 검증됨 → 경로 주입 불가


# ---- 시뮬레이터 DB (지연 임포트: google-auth 없는 환경에서도 모듈 로드 가능) ----
def _simdb():
    import importlib
    sys.path.insert(0, os.path.join(ROOT, ".."))
    return importlib.import_module("app.sim.db")


def _latest_asof():
    """daily_signals.json의 최신 거래일(시뮬 시작일 정렬용). 없으면 UTC 오늘."""
    import datetime as _dt
    sp = os.path.join(ROOT, "..", "state", "daily_signals.json")
    try:
        return json.load(open(sp, encoding="utf-8")).get("asof") or _dt.date.today().isoformat()
    except Exception:
        return _dt.date.today().isoformat()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _path(self):
        return self.path.split("?")[0].rstrip("/")

    def _auth(self):
        return verify(self.headers)   # {"sub","email"} 또는 None

    def _body(self):
        try:
            n = int((self.headers.get("Content-Length", "0") or "0").strip())
        except (ValueError, TypeError):
            return None
        if n < 0 or n > MAX_BODY:
            return None
        raw = self.rfile.read(n) if n else b"{}"
        try:
            d = json.loads(raw or b"{}")
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def do_GET(self):
        path = self._path()
        if path == "/api/userdata":
            info = self._auth()
            if not info:
                return self._send(401, {"error": "unauthorized"})
            p = path_for(info["sub"])
            try:
                data = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
            except Exception:
                data = {}
            return self._send(200, data)
        if path == "/api/sim":
            info = self._auth()
            if not info:
                return self._send(401, {"error": "unauthorized"})
            try:
                db = _simdb(); db.init()
                res = db.results(info["sub"]) or {"active": False}
            except Exception as e:
                return self._send(500, {"error": str(e)})
            return self._send(200, res)
        if path == "/api/backtest":
            return self._backtest()
        if path == "/api/quote":
            return self._quote()
        if path == "/api/stream":
            return self._stream()
        if path == "/api/dart":
            return self._dart()
        if path.startswith("/api/toss/"):
            try:
                if path == "/api/toss/status":
                    return self._toss_status()
                if path == "/api/toss/account":
                    return self._toss_account()
                if path == "/api/toss/market":
                    return self._toss_market()
                if path == "/api/toss/config":
                    return self._toss_config_get()
                if path == "/api/toss/audit":
                    return self._toss_audit()
                if path == "/api/toss/insight":
                    return self._toss_insight()
            except Exception as e:
                try:
                    return self._send(500, {"error": "서버 오류(" + type(e).__name__ + ")"})
                except Exception:
                    return
        return self._send(404, {"error": "not found"})

    def _dart(self):
        """종목별 DART 종합 프로파일(재무부실·희석·지분·공급계약·최근공시10) — 보유종목 화면용. 로그인 전용·6h 캐시."""
        import urllib.parse, re as _re, importlib
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        codes = [c for c in (q.get("codes") or [""])[0].split(",") if _re.match(r"^\d{6}$", c)][:12]
        if not codes:
            return self._send(400, {"error": "codes 필요(6자리, 최대 12)"})
        try:
            sys.path.insert(0, os.path.join(ROOT, ".."))
            F = importlib.import_module("app.dart.filter")
        except Exception as e:
            return self._send(503, {"error": f"dart unavailable: {str(e)[:120]}"})
        now = time.time()
        out = {}
        for c in codes:
            hit = _DART_CACHE.get(c)
            if hit and now - hit[0] < _DART_TTL:
                out[c] = hit[1]; continue
            try:
                prof = F.company_dart_profile(c, recent_n=10)
            except Exception as e:
                prof = {"ticker": c, "error": str(e)[:100]}
            _DART_CACHE[c] = (now, prof)
            out[c] = prof
        return self._send(200, out)

    def _stream(self):
        """SSE — KIS WS 틱 캐시(app.data.kis_ws.LATEST)를 보유종목별로 푸시. EventSource는 헤더 못 보내 ?token= 인증.
        장애/미지원 시 브라우저는 기존 4초 /api/quote 폴링으로 폴백(이 엔드포인트와 독립)."""
        import urllib.parse, re as _re, importlib
        global ACTIVE_STREAMS
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        if not verify_token((q.get("token") or [""])[0]):
            return self._send(401, {"error": "unauthorized"})
        codes = [c for c in (q.get("codes") or [""])[0].split(",") if _re.match(r"^\d{6}$", c)][:40]
        if not codes:
            return self._send(400, {"error": "codes 필요(6자리)"})
        try:
            sys.path.insert(0, os.path.join(ROOT, ".."))
            kis_ws = importlib.import_module("app.data.kis_ws")
            kis_ws.start()
        except Exception as e:
            return self._send(503, {"error": f"ws unavailable: {str(e)[:120]}"})
        with ACTIVE_LOCK:
            if ACTIVE_STREAMS >= MAX_STREAMS:
                return self._send(503, {"error": "too many streams"})
            ACTIVE_STREAMS += 1
        kis_ws.register(codes)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")     # nginx 버퍼링 방지(이중 안전장치)
            self.end_headers()
            self.wfile.write(b"retry: 4000\n\n"); self.wfile.flush()
            prev, last = None, time.monotonic()
            while True:
                snap = kis_ws.snapshot(codes)
                now = time.monotonic()
                if snap != prev:
                    self.wfile.write(b"event: price\ndata: " + json.dumps(snap).encode() + b"\n\n")
                    self.wfile.flush(); prev, last = snap, now
                elif now - last >= 20:
                    self.wfile.write(b": keepalive\n\n"); self.wfile.flush(); last = now
                time.sleep(1)                                # 1Hz로 캐시 폴링(WS는 더 빨리 LATEST 갱신, 합쳐서 전송)
        except (BrokenPipeError, ConnectionResetError):
            pass                                             # 클라이언트/nginx가 닫음 → 종료
        except Exception:
            pass
        finally:
            kis_ws.unregister(codes)
            with ACTIVE_LOCK:
                ACTIVE_STREAMS -= 1

    def _quote(self):
        """KIS 실시간 시세(읽기전용). 로그인 사용자만 — 공개 남용으로 KIS 쿼터 소모 방지."""
        import urllib.parse, re as _re, importlib
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        codes = [c for c in (q.get("codes") or [""])[0].split(",") if _re.match(r"^\d{6}$", c)][:40]
        if not codes:
            return self._send(400, {"error": "codes 필요(6자리, 최대 40)"})
        try:
            sys.path.insert(0, os.path.join(ROOT, ".."))
            kis = importlib.import_module("app.data.kis_api")
            return self._send(200, {"quotes": kis.get_prices(codes)})
        except Exception as e:
            return self._send(500, {"error": str(e)[:200]})

    def _backtest(self):
        import urllib.parse, subprocess, re as _re
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        g = lambda k, d: (q.get(k) or [d])[0]
        start, end = g("start", "2024-01-01"), g("end", "2024-12-31")
        if not (_re.match(r"^\d{4}-\d{2}-\d{2}$", start) and _re.match(r"^\d{4}-\d{2}-\d{2}$", end)):
            return self._send(400, {"error": "날짜 형식 오류(YYYY-MM-DD)"})
        try:
            cap = float(g("capital", "10000000")); mult = float(g("exposure_mult", "1.0")); cb = float(g("cb_limit", "0.03"))
        except ValueError:
            return self._send(400, {"error": "파라미터 오류"})
        if not (10000 <= cap <= 100_000_000_000):
            return self._send(400, {"error": "투자금 범위 오류"})
        strategy = g("strategy", "swing")
        if strategy == "overnight":           # 단타(오버나이트): 노출 비중·게이트만
            try:
                exposure = float(g("exposure", "0.3"))
            except ValueError:
                return self._send(400, {"error": "파라미터 오류"})
            gate = "on" if g("gate", "off") == "on" else "off"
            cmd = [sys.executable, "-m", "app.sim.overnight_cli", "--start", start, "--end", end,
                   "--capital", str(cap), "--exposure", str(max(0.0, min(1.0, exposure))), "--gate", gate]
        elif strategy == "index":             # 인덱스 로테이션(KOSPI/KOSDAQ 40주선 추세 + 스타일)
            mode = g("mode", "rotation")
            if mode not in ("rotation", "kospi_tf", "hold"):
                mode = "rotation"
            cmd = [sys.executable, "-m", "app.sim.index_backtest", "--start", start, "--end", end,
                   "--capital", str(cap), "--mode", mode]
        elif strategy == "coresat":           # 코어-위성: 코어(지수 로테이션) core_weight + 위성(active) 나머지
            try:
                cwt = max(0.0, min(1.0, float(g("core_weight", "0.7"))))
            except ValueError:
                return self._send(400, {"error": "파라미터 오류"})
            cmd = [sys.executable, "-m", "backtest.core_satellite_backtest", "--start", start, "--end", end,
                   "--capital", str(cap), "--core-weight", str(cwt), "--json"]
        else:
            cb_mode = "liq" if g("cb_mode", "block") == "liq" else "block"
            cmd = [sys.executable, "-m", "app.sim.backtest_cli", "--start", start, "--end", end,
                   "--capital", str(cap), "--exposure-mult", str(mult), "--cb-limit", str(cb), "--cb-mode", cb_mode]
        try:
            p = subprocess.run(cmd, cwd=os.path.join(ROOT, ".."), capture_output=True, text=True, timeout=60)
            lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
            out = json.loads(lines[-1]) if lines else {"error": "결과 없음"}
        except subprocess.TimeoutExpired:
            return self._send(504, {"error": "백테스트 시간 초과"})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        return self._send(200, out)

    def do_PUT(self):
        if self._path() != "/api/userdata":
            return self._send(404, {"error": "not found"})
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        data = self._body()
        if data is None:
            return self._send(400, {"error": "bad json or too large"})
        sub = info["sub"]
        tmp = path_for(sub) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path_for(sub))   # 원자적 교체
        self._send(200, {"ok": True})

    # ── 토스증권 연동 (시장데이터=소유자키 / 계좌·주문=사용자 본인키) ──
    def _toss_owner(self):
        return os.environ.get("TOSS_API_KEY"), os.environ.get("TOSS_SECRET_KEY")

    def _toss_link(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        data = self._body()
        if not data or not data.get("api_key") or not data.get("secret"):
            return self._send(400, {"error": "api_key/secret 필요"})
        try:
            from app.data import toss_api as T
            from server import toss_keystore as KS
        except Exception as e:
            return self._send(500, {"error": "toss module load: " + str(e)[:120]})
        api_key, secret = str(data["api_key"]).strip(), str(data["secret"]).strip()
        try:                                   # 키 유효성 = 계좌 조회로 검증
            acc = T.get_accounts(api_key, secret)
        except T.TossError as e:
            return self._send(400, {"error": "키 검증 실패(토스 인증 오류)", "status": e.status})
        except Exception as e:
            return self._send(502, {"error": "토스 연결 실패: " + str(e)[:100]})
        res = (acc or {}).get("result") or []
        if not res:
            return self._send(400, {"error": "계좌 없음 — 키/계좌 확인"})
        a0 = res[0]
        KS.save(info["sub"], api_key, secret, a0.get("accountSeq"), a0.get("accountNo"))
        return self._send(200, {"linked": True, "account_no_masked": "***" + str(a0.get("accountNo", ""))[-4:],
                                "account_type": a0.get("accountType"), "n_accounts": len(res)})

    def _toss_unlink(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        from server import toss_keystore as KS
        KS.delete(info["sub"])
        return self._send(200, {"linked": False})

    def _toss_status(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        from server import toss_keystore as KS
        return self._send(200, KS.status(info["sub"]))

    def _toss_account(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        from app.data import toss_api as T
        from server import toss_keystore as KS
        creds = KS.get(info["sub"])
        if not creds:
            return self._send(400, {"error": "미연동 — 토스 키를 먼저 연동하세요", "linked": False})
        api_key, secret, seq = creds
        out = {"linked": True}
        try:
            out["holdings"] = T.get_holdings(api_key, secret, seq)   # 핵심 — 실패 시만 에러
        except T.TossError as e:
            return self._send(502, {"error": "보유 조회 실패(잠시 후 재시도)", "status": e.status})
        for key, fn in (("open_orders", lambda: T.get_orders(api_key, secret, seq, status="OPEN")),
                        ("closed_orders", lambda: T.get_orders(api_key, secret, seq, status="CLOSED", limit=20)),
                        ("buying_power", lambda: T.get_buying_power(api_key, secret, seq)),
                        ("market_kr", lambda: T.get_market_calendar(api_key, secret, "KR"))):
            try:
                out[key] = fn()      # 개별 실패는 생략(일부 조회 실패가 전체를 막지 않게)
            except Exception:
                pass
        try:            # 보유종목 우리전략 신호(전 KRX, 유니버스 밖 포함)
            items = ((out.get("holdings") or {}).get("result") or {}).get("items") or []
            syms = [str(it.get("symbol")) for it in items if it.get("symbol")]
            if syms:
                from server import toss_analysis as TA
                out["signals"] = TA.signals(syms)
        except Exception:
            pass
        return self._send(200, out)

    def _toss_market(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        import urllib.parse
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        g = lambda k, d=None: (q.get(k) or [d])[0]
        ak, sk = self._toss_owner()
        if not ak or not sk:
            return self._send(503, {"error": "시장데이터 키 미설정"})
        from app.data import toss_api as T
        kind = g("kind", "prices")
        try:
            if kind == "info":       # 통합 시장정보: 환율·KR/US 장운영·금/국내외 시세
                out = {}
                for key, fn in (("fx_usd", lambda: T.get_exchange_rate(ak, sk, "USD", "KRW")),
                                ("fx_jpy", lambda: T.get_exchange_rate(ak, sk, "JPY", "KRW")),
                                ("market_kr", lambda: T.get_market_calendar(ak, sk, "KR")),
                                ("market_us", lambda: T.get_market_calendar(ak, sk, "US")),
                                ("prices", lambda: T.get_prices(ak, sk, "132030,069500,229200,005930,000660,AAPL,NVDA,TSLA,SPY,QQQ"))):
                    try:
                        out[key] = fn()
                    except Exception:
                        pass
                return self._send(200, out)
            if kind == "prices" and g("symbols"):
                return self._send(200, T.get_prices(ak, sk, g("symbols")))
            if kind == "candles" and g("symbol"):
                return self._send(200, T.get_candles(ak, sk, g("symbol"), interval=g("interval", "1d"), count=g("count")))
            if kind == "stocks" and g("symbols"):
                return self._send(200, T.get_stocks(ak, sk, g("symbols")))
            if kind == "orderbook" and g("symbol"):
                return self._send(200, T.get_orderbook(ak, sk, g("symbol")))
            return self._send(400, {"error": "kind/symbol(s) 확인 (kind=prices|candles|stocks|orderbook)"})
        except T.TossError as e:
            return self._send(502, {"error": "시장데이터 조회 실패", "status": e.status})

    def _toss_insight(self):
        """매매 인사이트 — 최근 체결(FIFO 매칭) 실현손익·승률·수수료/세금·매수매도·종목별."""
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        from app.data import toss_api as T
        from server import toss_keystore as KS
        creds = KS.get(info["sub"])
        if not creds:
            return self._send(400, {"error": "미연동"})
        api_key, secret, seq = creds
        try:
            orders = T.get_orders_paged(api_key, secret, seq, "CLOSED", pages=3, per=50)
        except T.TossError as e:
            return self._send(502, {"error": "주문 이력 조회 실패", "status": e.status})
        from collections import defaultdict, deque
        filled = [o for o in orders if o.get("status") == "FILLED" and o.get("execution")]
        filled.sort(key=lambda o: (o.get("execution") or {}).get("filledAt") or o.get("orderedAt") or "")
        n_buy = n_sell = 0
        buy_amt = sell_amt = fee = tax = 0.0
        q = defaultdict(deque)
        realized = defaultdict(float); rt = defaultdict(int); wins = defaultdict(int)
        for o in filled:
            ex = o.get("execution") or {}
            qty = float(ex.get("filledQuantity") or 0); px = float(ex.get("averageFilledPrice") or 0)
            fee += float(ex.get("commission") or 0); tax += float(ex.get("tax") or 0)
            amt = float(ex.get("filledAmount") or 0); sym = o.get("symbol")
            if o.get("side") == "BUY":
                n_buy += 1; buy_amt += amt; q[sym].append([qty, px])
            elif o.get("side") == "SELL":
                n_sell += 1; sell_amt += amt
                rem, pnl, matched = qty, 0.0, 0.0
                while rem > 1e-9 and q[sym]:
                    lot = q[sym][0]; m = min(rem, lot[0])
                    pnl += (px - lot[1]) * m; matched += m; lot[0] -= m; rem -= m
                    if lot[0] <= 1e-9:
                        q[sym].popleft()
                if matched > 0:
                    realized[sym] += pnl; rt[sym] += 1
                    if pnl > 0:
                        wins[sym] += 1
        total_rt = sum(rt.values()); total_wins = sum(wins.values())
        by = sorted(({"symbol": s, "realized": round(realized[s]), "trips": rt[s]} for s in realized),
                    key=lambda x: x["realized"])
        return self._send(200, {
            "summary": {"n_filled": len(filled), "n_buy": n_buy, "n_sell": n_sell,
                        "buy_amt": round(buy_amt), "sell_amt": round(sell_amt),
                        "net_amt": round(buy_amt - sell_amt), "fee": round(fee), "tax": round(tax),
                        "realized": round(sum(realized.values())), "round_trips": total_rt,
                        "win_rate": (total_wins / total_rt) if total_rt else 0.0,
                        "has_more": bool(orders and len(orders) >= 150)},
            "by_symbol": (by[:5] + by[-5:]) if len(by) > 10 else by,
            "recent": orders[:15],
        })

    def _toss_config_get(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        from server import toss_config as CFG
        return self._send(200, CFG.get_config(info["sub"]))

    def _toss_audit(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        from server import toss_config as CFG
        return self._send(200, {"audit": CFG.get_audit(info["sub"], 50)})

    def _toss_config_set(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        data = self._body() or {}
        import datetime as _dt
        from server import toss_config as CFG
        sub = info["sub"]
        cur = CFG.get_config(sub)
        upd, risk_up = {}, False
        if "kill_switch" in data:
            v = bool(data["kill_switch"]); upd["kill_switch"] = v
            if cur.get("kill_switch") and not v:        # 킬스위치 해제 = 위험 증가
                risk_up = True
        for k, lo, hi in (("max_notional_krw", 0, 100_000_000),        # 상한 1억으로 하향(실계좌 규모)
                          ("program_max_notional_krw", 0, 100_000_000),
                          ("program_max_positions", 0, 50), ("program_daily_order_cap", 0, 200)):
            if k in data:
                try:
                    v = max(lo, min(hi, float(data[k]) if "." in str(data[k]) else int(data[k])))
                except Exception:
                    continue
                upd[k] = v
                if v > float(cur.get(k, 0)):            # 한도 상향 = 위험 증가
                    risk_up = True
        for k in ("program_enabled", "program_dry_run"):
            if k in data:
                v = bool(data[k]); upd[k] = v
                if k == "program_enabled" and v and not cur.get(k):
                    risk_up = True
                if k == "program_dry_run" and (not v) and cur.get(k):   # 드라이런 해제 = 실주문 활성
                    risk_up = True
        if risk_up and data.get("confirm") is not True:
            return self._send(400, {"error": "위험 증가 설정(킬스위치 해제·한도 상향·프로그램/실주문 활성)은 confirm=true 필요", "need_confirm": True})
        newc = CFG.set_config(sub, **upd)
        CFG.log_order(sub, {"ts": _dt.datetime.now().isoformat(), "symbol": "-", "side": "", "result": "config",
                            "detail": "변경 " + ",".join(sorted(upd.keys())), "src": "config"})
        return self._send(200, newc)

    def _toss_order(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        data = self._body()
        if not data:
            return self._send(400, {"error": "bad json"})
        import datetime as _dt
        from app.data import toss_api as T
        from server import toss_keystore as KS, toss_config as CFG
        sub = info["sub"]
        creds = KS.get(sub)
        if not creds:
            return self._send(400, {"error": "미연동 — 토스 키를 먼저 연동하세요"})
        api_key, secret, seq = creds
        cfg = CFG.get_config(sub)
        symbol = str(data.get("symbol", "")).strip()
        side = str(data.get("side", "")).upper()
        otype = str(data.get("order_type", "LIMIT")).upper()
        amt = data.get("order_amount")
        # 엄격 입력검증(fail-closed) — 예외를 삼켜 통과시키지 않음
        if data.get("confirm") is not True:
            return self._send(400, {"error": "주문 확인(confirm=true) 필요"})
        if not symbol or side not in ("BUY", "SELL") or otype not in ("LIMIT", "MARKET"):
            return self._send(400, {"error": "symbol/side(BUY|SELL)/order_type(LIMIT|MARKET) 확인"})
        qty = price = amtf = None
        if amt is not None:
            try:
                amtf = float(amt)
            except (TypeError, ValueError):
                return self._send(400, {"error": "주문금액(order_amount)이 올바르지 않습니다"})
            if amtf <= 0:
                return self._send(400, {"error": "주문금액은 양수여야 합니다"})
        else:
            try:
                qty = int(data.get("quantity"))
            except (TypeError, ValueError):
                return self._send(400, {"error": "수량은 양의 정수여야 합니다"})
            if qty <= 0:
                return self._send(400, {"error": "수량은 양수여야 합니다"})
            if otype == "LIMIT":
                try:
                    price = float(data.get("price"))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "지정가(price)를 입력하세요"})
                if price <= 0:
                    return self._send(400, {"error": "지정가는 양수여야 합니다"})
        ts = _dt.datetime.now().isoformat()
        today = ts[:10]
        base = {"ts": ts, "symbol": symbol, "side": side, "order_type": otype, "quantity": qty, "price": price, "src": "manual"}
        if cfg.get("kill_switch"):
            CFG.log_order(sub, {**base, "result": "blocked", "detail": "kill_switch ON"})
            return self._send(403, {"error": "킬스위치 ON — 모든 주문 차단(설정에서 해제)"})
        if CFG.daily_order_count(sub, today) >= int(cfg.get("program_daily_order_cap", 20)):
            CFG.log_order(sub, {**base, "result": "blocked", "detail": "일일 주문 캡"})
            return self._send(429, {"error": "일일 주문 캡 도달"})
        # 명목가 한도(KRW) — fail-closed: 계산 성공 시에만 통과, 못 구하면 거부. USD는 보수적 환산.
        FX = 1500.0
        notional_krw = None
        try:
            pr = T.get_prices(api_key, secret, symbol)["result"][0]
            ccy = pr.get("currency", "KRW"); cur_px = float(pr["lastPrice"])
            mult = 1.0 if ccy == "KRW" else FX
            if amtf is not None:
                notional_krw = amtf * (1.0 if ccy == "KRW" else FX)
            elif otype == "LIMIT":
                notional_krw = qty * price * mult
            else:
                notional_krw = qty * cur_px * mult
        except Exception:
            notional_krw = None
        if notional_krw is None:
            CFG.log_order(sub, {**base, "result": "blocked", "detail": "한도검증 불가(시세)"})
            return self._send(403, {"error": "한도 검증 불가(시세 조회 실패) — 안전상 주문 거부"})
        if notional_krw > float(cfg.get("max_notional_krw", 0)):
            CFG.log_order(sub, {**base, "result": "blocked", "detail": f"명목가 {int(notional_krw)} > 한도 {int(cfg['max_notional_krw'])}"})
            return self._send(403, {"error": f"단건 한도 초과: 약 {int(notional_krw):,}원 > {int(cfg['max_notional_krw']):,}원 (설정에서 조정)"})
        CFG.bump_daily(sub, today)
        try:
            resp = T.create_order(api_key, secret, seq, symbol, side, otype,
                                  quantity=qty, price=price, order_amount=amtf,
                                  client_order_id=data.get("client_order_id"))
            oid = (resp.get("result") or {}).get("orderId")
            CFG.log_order(sub, {**base, "result": "placed", "order_id": oid, "notional": int(notional_krw)})
            return self._send(200, {"ok": True, "order": resp})
        except T.TossError as e:                                   # e.body 미반환·미저장(키 유출 방지)
            CFG.log_order(sub, {**base, "result": "error", "detail": f"토스 거부(HTTP {e.status})"})
            return self._send(502, {"error": "주문 실패", "status": e.status})

    def _toss_cancel(self):
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        data = self._body() or {}
        oid = str(data.get("order_id", "")).strip()
        if not oid:
            return self._send(400, {"error": "order_id 필요"})
        from app.data import toss_api as T
        from server import toss_keystore as KS
        creds = KS.get(info["sub"])
        if not creds:
            return self._send(400, {"error": "미연동"})
        api_key, secret, seq = creds
        try:
            return self._send(200, {"ok": True, "result": T.cancel_order(api_key, secret, seq, oid)})
        except T.TossError as e:
            return self._send(502, {"error": "취소 실패", "status": e.status})

    def _toss_program_run(self):
        """3단계 프로그램매매 1회 — 현 신호(daily_signals.buy_order) 미보유분을 한도 내 지정가 매수.
        안전: program_enabled·kill_switch·일일캡·최대포지션·단건한도·드라이런 기본. 매수 전용(v1)."""
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        data = self._body() or {}
        import datetime as _dt, math as _math, threading as _th
        from app.data import toss_api as T
        from server import toss_keystore as KS, toss_config as CFG
        sub = info["sub"]
        gl = globals()
        with gl.setdefault("_toss_prog_guard", _th.Lock()):
            lk = gl.setdefault("_toss_prog_locks", {}).setdefault(sub, _th.Lock())
        if not lk.acquire(blocking=False):          # 동시 실행 차단(중복 발주 방지)
            return self._send(429, {"error": "프로그램 매매가 이미 실행 중입니다"})
        try:
            creds = KS.get(sub)
            if not creds:
                return self._send(400, {"error": "미연동"})
            api_key, secret, seq = creds
            cfg = CFG.get_config(sub)
            if not cfg.get("program_enabled"):
                return self._send(400, {"error": "프로그램매매 비활성 — 설정에서 켜세요"})
            if cfg.get("kill_switch"):
                return self._send(403, {"error": "킬스위치 ON — 차단"})
            dry = bool(data.get("dry_run", cfg.get("program_dry_run", True)))
            today = _dt.date.today().isoformat()
            cap = int(cfg.get("program_daily_order_cap", 20))
            if CFG.daily_order_count(sub, today) >= cap:
                return self._send(429, {"error": "일일 주문 캡 도달"})
            sigp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "daily_signals.json")
            try:
                sig = json.load(open(sigp, encoding="utf-8"))
            except Exception:
                return self._send(500, {"error": "신호 로드 실패"})
            buy_syms = [b.get("ticker") for b in (sig.get("buy_order") or []) if b.get("ticker")]
            try:                                    # 보유 + 미체결 매수(둘 다 포지션 점유로 간주)
                items = ((T.get_holdings(api_key, secret, seq) or {}).get("result") or {}).get("items") or []
                openo = (((T.get_orders(api_key, secret, seq, status="OPEN") or {}).get("result") or {}).get("orders")) or []
            except T.TossError as e:
                return self._send(502, {"error": "보유/주문 조회 실패", "status": e.status})
            held = {str(it.get("symbol")) for it in items}
            reserved = held | {str(o.get("symbol")) for o in openo}
            cap_notional = float(cfg.get("program_max_notional_krw", 2_000_000))
            max_pos = int(cfg.get("program_max_positions", 5))
            room = max(0, max_pos - len(reserved))
            plan, results = [], []
            for tk in buy_syms:
                if tk in reserved or len(plan) >= room:
                    continue
                try:
                    prc = T.get_prices(api_key, secret, tk)["result"][0]
                    px = float(prc["lastPrice"]); mult = 1.0 if prc.get("currency", "KRW") == "KRW" else 1500.0
                except Exception:
                    results.append({"symbol": tk, "result": "skip", "detail": "시세 조회 실패"}); continue
                qty = int(_math.floor(cap_notional / (px * mult))) if px > 0 else 0
                if qty < 1:
                    results.append({"symbol": tk, "result": "skip", "detail": "1주 명목가 > 한도"}); continue
                plan.append({"symbol": tk, "side": "BUY", "order_type": "LIMIT", "quantity": qty, "price": int(px)})
            # 매도(청산) — 보유종목 중 우리 청산규칙(20주선 종가 이탈) 신호
            try:
                from server import toss_analysis as TA
                sigs = TA.signals([str(it.get("symbol")) for it in items])
            except Exception:
                sigs = {}
            for it in items:
                sym = str(it.get("symbol")); sg = sigs.get(sym) or {}
                wma, cl = sg.get("wma20"), sg.get("close")
                try:
                    qh = int(float(it.get("quantity") or 0))
                except Exception:
                    qh = 0
                if qh >= 1 and wma and cl and cl < wma:      # 20주선 이탈 = 청산 신호
                    plan.append({"symbol": sym, "side": "SELL", "order_type": "LIMIT",
                                 "quantity": qh, "price": int(cl), "reason": "20주선 이탈"})
            ts = _dt.datetime.now().isoformat()
            for p in plan:
                b = {"ts": ts, **p, "src": "program"}
                if dry:
                    CFG.log_order(sub, {**b, "result": "dry_run"})
                    results.append({**p, "result": "dry_run"}); continue
                if CFG.daily_order_count(sub, today) >= cap:
                    results.append({**p, "result": "skip", "detail": "일일캡"}); break
                CFG.bump_daily(sub, today)          # API 발주 전 카운트(경쟁조건·폭주 방지)
                try:
                    resp = T.create_order(api_key, secret, seq, p["symbol"], p["side"], p.get("order_type", "LIMIT"),
                                          quantity=p["quantity"], price=p["price"],
                                          client_order_id=f"prog_{today}_{p['side']}_{p['symbol']}")
                    oid = (resp.get("result") or {}).get("orderId")
                    CFG.log_order(sub, {**b, "result": "placed", "order_id": oid})
                    results.append({**p, "result": "placed", "order_id": oid})
                except T.TossError as e:            # 본문 미저장·미반환
                    CFG.log_order(sub, {**b, "result": "error", "detail": f"토스 거부(HTTP {e.status})"})
                    results.append({**p, "result": "error", "status": e.status})
            return self._send(200, {"dry_run": dry, "signals": buy_syms, "held": sorted(held),
                                    "open": sorted({str(o.get("symbol")) for o in openo}),
                                    "max_positions": max_pos, "results": results})
        finally:
            lk.release()

    def do_POST(self):
        path = self._path()
        if path.startswith("/api/toss/"):
            try:
                if path == "/api/toss/program/run":
                    return self._toss_program_run()
                if path == "/api/toss/link":
                    return self._toss_link()
                if path == "/api/toss/unlink":
                    return self._toss_unlink()
                if path == "/api/toss/order":
                    return self._toss_order()
                if path == "/api/toss/cancel":
                    return self._toss_cancel()
                if path == "/api/toss/config":
                    return self._toss_config_set()
                return self._send(404, {"error": "not found"})
            except Exception as e:
                try:
                    return self._send(500, {"error": "서버 오류(" + type(e).__name__ + ")"})
                except Exception:
                    return
        if path not in ("/api/sim/start", "/api/sim/reset"):
            return self._send(404, {"error": "not found"})
        info = self._auth()
        if not info:
            return self._send(401, {"error": "unauthorized"})
        data = self._body()
        if data is None:
            return self._send(400, {"error": "bad json"})
        db = _simdb(); db.init()
        sub = info["sub"]
        try:
            cur = db.get_sim(sub) if path == "/api/sim/reset" else None
            inv = float(data.get("investment") or (cur and cur["investment"]) or 0)
            if not (10000 <= inv <= 100_000_000_000):     # 1만~1000억 범위
                return self._send(400, {"error": "investment out of range"})
            def _num(key, default, lo, hi):
                v = data.get(key)
                if v is None:
                    return float((cur and cur[key]) if cur and cur[key] is not None else default)
                v = float(v)
                return max(lo, min(hi, v))
            cb_limit = _num("cb_limit", 0.03, 0.0, 0.20)         # 0=끔 ~ 20%
            exposure_mult = _num("exposure_mult", 1.0, 0.5, 2.5)  # 공격성 배수
            _mode = data.get("cb_mode")
            if _mode is None and cur is not None:
                _mode = cur.get("cb_mode")
            cb_mode = "liq" if _mode == "liq" else "block"        # 청산 방식: block(신규중단) | liq(전량청산)
            core_weight = _num("core_weight", 0.7, 0.0, 1.0)      # 코어-위성: 코어(지수 로테이션) 비중. 기본 0.7. 0=순수 active
            db.start_sim(sub, info.get("email", ""), inv, _latest_asof(),
                         cb_limit=cb_limit, exposure_mult=exposure_mult, cb_mode=cb_mode, core_weight=core_weight)
            return self._send(200, db.results(sub) or {"active": True})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass   # 접근 로그 억제


if __name__ == "__main__":
    if _gid is None:
        print("경고: google-auth 미설치 — 토큰 검증 불가(전부 401). pip install google-auth", file=sys.stderr)
    if not CLIENT_ID:
        print("경고: GOOGLE_CLIENT_ID 미설정 — 전부 401", file=sys.stderr)
    print(f"sync_api 구동 127.0.0.1:{PORT} · data={os.path.abspath(DATA_DIR)}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
