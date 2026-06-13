"""LEADERS DESK 동기화 API — 구글 ID 토큰 검증 후 사용자별 데이터 저장/조회 (자체 호스팅).

- 인증: 클라이언트가 구글 로그인으로 받은 ID 토큰을 Authorization: Bearer 로 전달.
        google-auth로 서명·만료·issuer·audience(=GOOGLE_CLIENT_ID) 로컬 검증 → 사용자 고유 sub 추출.
- 저장: state/userdata/{sub}.json (gitignored). 본인 sub로만 read/write — 교차 접근 불가.
- 배포: nginx `/trading/api/` → 127.0.0.1:SYNC_PORT, systemd로 상시 구동.
- 의존성: google-auth (requirements). 웹 프레임워크 없이 stdlib http.server.

환경변수: GOOGLE_CLIENT_ID(필수, 공개값) · SYNC_PORT(기본 8799)
"""
from __future__ import annotations
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def verify(headers):
    """ID 토큰 검증 → 사용자 sub(숫자 문자열) 또는 None."""
    if _gid is None or not CLIENT_ID:
        return None
    auth = headers.get("Authorization", "")
    tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not tok:
        return None
    try:
        info = _gid.verify_oauth2_token(tok, _REQ, CLIENT_ID)
        if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            return None
        sub = str(info.get("sub", ""))
        return sub if sub.isdigit() else None
    except Exception:
        return None


def path_for(sub):
    return os.path.join(DATA_DIR, f"{sub}.json")     # sub는 숫자 검증됨 → 경로 주입 불가


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route_ok(self):
        return self.path.split("?")[0].rstrip("/") == "/api/userdata"

    def do_GET(self):
        if not self._route_ok():
            return self._send(404, {"error": "not found"})
        sub = verify(self.headers)
        if not sub:
            return self._send(401, {"error": "unauthorized"})
        p = path_for(sub)
        try:
            data = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
        except Exception:
            data = {}
        self._send(200, data)

    def do_PUT(self):
        if not self._route_ok():
            return self._send(404, {"error": "not found"})
        sub = verify(self.headers)
        if not sub:
            return self._send(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "too large"})
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self._send(400, {"error": "bad json"})
        tmp = path_for(sub) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path_for(sub))   # 원자적 교체
        self._send(200, {"ok": True})

    def log_message(self, *a):
        pass   # 접근 로그 억제


if __name__ == "__main__":
    if _gid is None:
        print("경고: google-auth 미설치 — 토큰 검증 불가(전부 401). pip install google-auth", file=sys.stderr)
    if not CLIENT_ID:
        print("경고: GOOGLE_CLIENT_ID 미설정 — 전부 401", file=sys.stderr)
    print(f"sync_api 구동 127.0.0.1:{PORT} · data={os.path.abspath(DATA_DIR)}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
