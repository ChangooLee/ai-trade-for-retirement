"""토스 연동 서버 셋업 — .env에 TOSS_MASTER_KEY(신규 생성)·TOSS_API_KEY·TOSS_SECRET_KEY 추가 + 검증.
소유자 키는 /tmp/.toss_owner.env(SFTP 전달)에서 읽어 .env에 이관 후 삭제. 값은 절대 출력 안 함.
사용: .venv/bin/python scripts/_toss_setup.py
"""
import os, sys
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
ENV = os.path.join(_REPO, ".env")
OWNER = "/tmp/.toss_owner.env"


def parse_env(path):
    d = {}
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    return d


own = parse_env(OWNER)
existing = parse_env(ENV)
from cryptography.fernet import Fernet
add = []
if "TOSS_MASTER_KEY" not in existing:
    add.append(("TOSS_MASTER_KEY", Fernet.generate_key().decode()))
if "TOSS_API_KEY" not in existing and own.get("TOSS_API_KEY"):
    add.append(("TOSS_API_KEY", own["TOSS_API_KEY"]))
if "TOSS_SECRET_KEY" not in existing and own.get("TOSS_SECRET_KEY"):
    add.append(("TOSS_SECRET_KEY", own["TOSS_SECRET_KEY"]))
if add:
    with open(ENV, "a", encoding="utf-8") as f:
        for k, v in add:
            f.write(f"\n{k}={v}")
    os.chmod(ENV, 0o600)
print("[.env] 추가 키(값 제외):", [k for k, _ in add] or "이미 모두 존재")
if os.path.exists(OWNER):
    os.remove(OWNER)
    print("[정리] /tmp/.toss_owner.env 삭제")

# 검증: env 로드 → 키저장소 암호화 라운드트립(더미) + 소유자키 시장/계좌 조회
merged = parse_env(ENV)
for k in ("TOSS_MASTER_KEY", "TOSS_API_KEY", "TOSS_SECRET_KEY"):
    if merged.get(k):
        os.environ[k] = merged[k]

from server import toss_keystore as KS
from app.data import toss_api as T
KS.save("__setup_test__", "dummy_key_abc", "dummy_secret_xyz", account_seq=999, account_no="0000001234")
g = KS.get("__setup_test__")
rt = (g is not None and g[0] == "dummy_key_abc" and g[1] == "dummy_secret_xyz" and g[2] == 999)
print("[키저장소] 암호화 라운드트립:", "✅ 통과" if rt else "❌ 실패", "· status=", KS.status("__setup_test__"))
KS.delete("__setup_test__")

ok = False
try:
    acc = T.get_accounts(os.environ["TOSS_API_KEY"], os.environ["TOSS_SECRET_KEY"])
    ok = isinstance(acc, dict) and bool(acc.get("result"))
except Exception as e:
    print("[소유자키] 계좌조회 예외:", type(e).__name__, str(e)[:120])
print("[소유자키] 시장데이터용 인증:", "✅ 정상(계좌 응답)" if ok else "확인 필요")
