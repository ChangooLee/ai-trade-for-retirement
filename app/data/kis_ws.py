"""KIS 실시간 체결가 WebSocket(H0STCNT0) — ★읽기전용★. 서버가 1개 WS를 상시 물고 틱을 LATEST 캐시에 적재.
브라우저는 sync_api의 SSE(/api/stream)로 이 캐시를 받아 보유 평가손익을 틱 단위로 갱신한다.

- 인증: POST /oauth2/Approval → approval_key(REST access_token과 별개, 24h 캐시). ★필드명 'secretkey'★.
- 구독: 모든 SSE 클라이언트가 요청한 종목의 합집합(refcount)을 1개 소켓으로 구독(≤SUB_CAP).
- 프레임: '0|H0STCNT0|001|<code>^<time>^<price>^...'(평문, 46필드/레코드, ^구분). PINGPONG은 그대로 에코(필수).
- 주문/체결통보(H0STCNI0)·호가(H0STASP0) 미구독 — AGENTS.md §2 읽기전용 유지.
"""
from __future__ import annotations
import json, sys, threading, time
import requests

sys.path.insert(0, ".")
from app.data import kis_api as _kis  # noqa: E402  (_load_env 재사용)

WS_URL = "ws://ops.koreainvestment.com:21000"          # 실전 도메인 실시간(평문, 라우팅은 body tr_id)
BASE = _kis.BASE
SUB_CAP = 40                                            # 보수적 상한(KIS 종목수 제한 버전의존 ~20~41). 초과는 skip+로그.
_FIELD_N = 46                                           # H0STCNT0 레코드당 필드 수(다중 레코드 슬라이스용)
#  필드 인덱스: 0=종목코드 1=체결시간 2=현재가 3=전일대비부호 4=전일대비 5=전일대비율

LATEST: dict = {}                                       # {code: {"price":int,"chg":float,"time":"HHMMSS","ts":float}}
_LATEST_LOCK = threading.Lock()
_REFCOUNT: dict = {}                                    # {code: 활성 SSE 클라이언트 수}
_DESIRED: set = set()                                   # 구독 목표(refcount>0인 코드)
_SUB_LOCK = threading.Lock()
_APPROVAL = {"key": None, "ts": 0.0}
_STARTED = False
_START_LOCK = threading.Lock()
_STOP = threading.Event()


def approval_key() -> str:
    if _APPROVAL["key"] and time.time() - _APPROVAL["ts"] < 86400:
        return _APPROVAL["key"]
    key, sec = _kis._load_env()
    r = requests.post(f"{BASE}/oauth2/Approval",
                      json={"grant_type": "client_credentials", "appkey": key, "secretkey": sec}, timeout=15)
    r.raise_for_status()
    _APPROVAL["key"] = r.json()["approval_key"]; _APPROVAL["ts"] = time.time()
    return _APPROVAL["key"]


def _frame(ak: str, code: str, tr_type: str) -> str:
    return json.dumps({"header": {"approval_key": ak, "custtype": "P", "tr_type": tr_type, "content-type": "utf-8"},
                       "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}}})


def register(codes) -> None:
    """SSE 클라이언트가 보는 종목 등록(refcount++). 합집합이 WS 구독 목표가 됨. SUB_CAP 초과는 skip."""
    with _SUB_LOCK:
        for c in codes:
            if _REFCOUNT.get(c, 0) == 0:
                if len(_DESIRED) >= SUB_CAP:
                    _log(f"SUB_CAP({SUB_CAP}) 초과 — {c} 구독 skip(해당 종목은 EOD 폴백)")
                    continue
                _DESIRED.add(c)
            _REFCOUNT[c] = _REFCOUNT.get(c, 0) + 1


def unregister(codes) -> None:
    with _SUB_LOCK:
        for c in codes:
            n = _REFCOUNT.get(c, 0) - 1
            if n <= 0:
                _REFCOUNT.pop(c, None); _DESIRED.discard(c)
            else:
                _REFCOUNT[c] = n


def snapshot(codes) -> dict:
    with _LATEST_LOCK:
        return {c: LATEST.get(c) for c in codes}


def _log(msg: str) -> None:
    print(f"[kis_ws] {msg}", file=sys.stderr)


def _handle(raw: str, ws) -> None:
    if raw.startswith("{"):                              # 제어/JSON 프레임
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("header", {}).get("tr_id") == "PINGPONG":
            ws.send(raw)                                 # ★PINGPONG 그대로 에코(미에코 시 연결 끊김)★
            return
        body = msg.get("body", {})
        if body.get("rt_cd") not in ("0", None):         # 구독 실패(예: 종목수 초과)
            _log(f"구독 ACK rt_cd={body.get('rt_cd')} {body.get('msg1')}")
        return
    parts = raw.split("|", 3)                            # 실시간 데이터: enc|tr_id|count|payload
    if len(parts) < 4 or parts[1] != "H0STCNT0":
        return
    try:
        cnt = int(parts[2])
    except ValueError:
        cnt = 1
    toks = parts[3].split("^")
    now = time.monotonic()
    upd = {}
    for i in range(max(1, cnt)):
        rec = toks[i * _FIELD_N:(i + 1) * _FIELD_N]
        if len(rec) < 6:
            continue
        try:
            upd[rec[0]] = {"price": int(rec[2]), "chg": float(rec[5] or 0), "time": rec[1], "ts": now}
        except (ValueError, IndexError):
            continue
    if upd:
        with _LATEST_LOCK:
            LATEST.update(upd)


def _run() -> None:
    from websocket import create_connection, WebSocketTimeoutException  # 지연 임포트
    backoff = 1
    while not _STOP.is_set():
        with _SUB_LOCK:
            want = bool(_DESIRED)
        if not want:                                     # 보는 사람 없으면 연결 안 함(유휴 연결 회피)
            time.sleep(2); continue
        ws = None
        try:
            ak = approval_key()
            ws = create_connection(WS_URL, timeout=10)
            ws.settimeout(1.0)
            subscribed: set = set()
            _log(f"WS 연결 — 구독 {len(_DESIRED)}종목")
            session_start = time.monotonic()
            while not _STOP.is_set():
                with _SUB_LOCK:
                    desired = set(_DESIRED)
                if not desired:
                    break                                # 아무도 안 봄 → 연결 종료
                for c in desired - subscribed:
                    ws.send(_frame(ak, c, "1")); subscribed.add(c)
                for c in subscribed - desired:
                    ws.send(_frame(ak, c, "2")); subscribed.discard(c)
                try:
                    raw = ws.recv()
                except WebSocketTimeoutException:
                    continue
                if raw:
                    _handle(raw, ws)
                if time.monotonic() - session_start > 60:
                    backoff = 1                          # 안정 세션 → 백오프 리셋
        except Exception as e:
            _log(f"WS 오류: {str(e)[:120]} — {backoff}s 후 재연결")
            time.sleep(backoff + (hash(str(e)) % 3))     # 지터(Math.random 불가 환경 대비)
            backoff = min(backoff * 2, 30)
        finally:
            try:
                if ws: ws.close()
            except Exception:
                pass


def start() -> None:
    """WS 데몬 스레드 1회 기동(멱등). sync_api가 모듈 로드/스트림 시 호출."""
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return
        t = threading.Thread(target=_run, name="kis-ws", daemon=True)
        t.start()
        _STARTED = True
        _log("데몬 스레드 기동")


if __name__ == "__main__":   # 핸드셰이크/틱 실측: python -m app.data.kis_ws 005930 [000660 ...]
    codes = sys.argv[1:] or ["005930"]
    register(codes); start()
    print(f"구독: {codes} — 14초 수신(장중이면 틱, 장마감이면 핸드셰이크만)", file=sys.stderr)
    for _ in range(14):
        time.sleep(1)
        print("  LATEST:", snapshot(codes), file=sys.stderr)
