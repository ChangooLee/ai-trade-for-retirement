"""토스 키 저장소 — 구글 계정(sub)별 토스 api_key/secret을 암호화 저장.

보안 원칙:
- 저장 시 Fernet 대칭암호(마스터키는 서버 .env `TOSS_MASTER_KEY`, 저장소 파일에 없음)로 at-rest 암호화.
- 파일 권한 0600. 키/토큰/평문은 절대 로깅·응답에 노출하지 않는다(status()는 마스킹 정보만).
- 각 sub는 '본인' 키만 저장/사용 — 주문·계좌는 요청자 sub의 키로만 (서버가 강제).
- 시장데이터용 소유자 키는 여기 저장 안 하고 .env(TOSS_API_KEY/TOSS_SECRET_KEY)에서 직접 사용.

주의: 타인의 주문 권한 키를 호스팅하는 것은 무거운 책임. 마스터키 유출 시 전 사용자 키 노출.
"""
from __future__ import annotations
import json, os, threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_STORE = os.path.join(_HERE, "..", "state", "toss_keys.json")
_lock = threading.Lock()


def _fernet():
    from cryptography.fernet import Fernet   # 미설치 시 ImportError → 상위에서 설치 안내
    mk = os.environ.get("TOSS_MASTER_KEY")
    if not mk:
        raise RuntimeError("TOSS_MASTER_KEY 미설정(.env) — 키 암호화 불가")
    return Fernet(mk.encode() if isinstance(mk, str) else mk)


def _load():
    try:
        return json.load(open(_STORE, encoding="utf-8"))
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # 0600 권한
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, _STORE)
    try:
        os.chmod(_STORE, 0o600)
    except OSError:
        pass


def save(sub, api_key, secret, account_seq=None, account_no=None):
    """sub의 토스 키 암호화 저장(기존 있으면 교체)."""
    f = _fernet()
    with _lock:
        d = _load()
        d[sub] = {
            "k": f.encrypt(api_key.encode()).decode(),
            "s": f.encrypt(secret.encode()).decode(),
            "account_seq": account_seq,
            "account_no": account_no,        # 표시는 마스킹해서만
        }
        _save(d)


def get(sub):
    """(api_key, secret, account_seq) 복호화 반환. 없으면 None. — 서버 내부에서만 사용."""
    with _lock:
        e = _load().get(sub)
    if not e:
        return None
    f = _fernet()
    return f.decrypt(e["k"].encode()).decode(), f.decrypt(e["s"].encode()).decode(), e.get("account_seq")


def delete(sub):
    with _lock:
        d = _load()
        if sub in d:
            del d[sub]
            _save(d)
            return True
    return False


def status(sub):
    """연동 여부 + 마스킹 계좌번호(비밀 없음) — 프런트 응답용."""
    with _lock:
        e = _load().get(sub)
    if not e:
        return {"linked": False}
    ano = e.get("account_no") or ""
    masked = ("***" + ano[-4:]) if ano else None
    return {"linked": True, "account_no_masked": masked, "account_seq": e.get("account_seq")}
