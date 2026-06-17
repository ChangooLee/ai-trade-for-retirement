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
        strategy = "overnight" if g("strategy", "swing") == "overnight" else "swing"
        if strategy == "overnight":           # 단타(오버나이트): 노출 비중·게이트만
            try:
                exposure = float(g("exposure", "0.3"))
            except ValueError:
                return self._send(400, {"error": "파라미터 오류"})
            gate = "on" if g("gate", "off") == "on" else "off"
            cmd = [sys.executable, "-m", "app.sim.overnight_cli", "--start", start, "--end", end,
                   "--capital", str(cap), "--exposure", str(max(0.0, min(1.0, exposure))), "--gate", gate]
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

    def do_POST(self):
        path = self._path()
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
            db.start_sim(sub, info.get("email", ""), inv, _latest_asof(),
                         cb_limit=cb_limit, exposure_mult=exposure_mult, cb_mode=cb_mode)
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
