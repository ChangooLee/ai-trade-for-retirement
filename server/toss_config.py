"""토스 주문 안전설정 + 감사로그 (구글 sub별, 비밀 아님 — 키는 toss_keystore).

kill_switch: 켜면 모든 주문 차단(수동·프로그램). max_notional_krw: 단건 주문 명목가 상한.
program_*: 3단계 프로그램매매 설정(기본 비활성·드라이런). 감사로그: 모든 주문 시도 기록(체결/거부 포함).
"""
from __future__ import annotations
import json, os, threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_STORE = os.path.join(_HERE, "..", "state", "toss_config.json")
_lock = threading.Lock()

DEFAULTS = {
    "kill_switch": False,              # True면 전 주문 차단
    "max_notional_krw": 3_000_000,     # 단건 주문 명목가(수량×가격) 상한(원). 보수적 기본.
    "core_weight": 0.7,                # 코어(글로벌 로테이션 ETF) 목표 비중 — 재배치 계획 기준
    "program_enabled": False,          # 3단계 프로그램매매 on/off
    "program_dry_run": True,           # 기본 드라이런(실주문 안 냄, 로그만)
    "program_max_positions": 5,        # 동시 보유 종목 수 상한
    "program_max_notional_krw": 2_000_000,   # 프로그램 단건 상한
    "program_daily_order_cap": 20,     # 하루 최대 주문 수(폭주 방지)
}
_ALLOWED = set(DEFAULTS.keys())
_AUDIT_CAP = 300


def _load():
    try:
        return json.load(open(_STORE, encoding="utf-8"))
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, _STORE)


def get_config(sub):
    with _lock:
        c = _load().get(sub, {}).get("config", {})
    return {**DEFAULTS, **c}


def set_config(sub, **kw):
    with _lock:
        d = _load()
        node = d.setdefault(sub, {})
        cfg = node.setdefault("config", {})
        for k, v in kw.items():
            if k in _ALLOWED:
                cfg[k] = v
        _save(d)
    return get_config(sub)


def log_order(sub, entry):
    """주문 시도 감사 기록(비밀 없음). entry: dict(ts·symbol·side·qty·price·result·detail)."""
    with _lock:
        d = _load()
        node = d.setdefault(sub, {})
        log = node.setdefault("audit", [])
        log.insert(0, entry)
        del log[_AUDIT_CAP:]
        _save(d)


def get_audit(sub, limit=50):
    with _lock:
        log = _load().get(sub, {}).get("audit", [])
    return log[:limit]


def bump_daily(sub, day):
    """당일 실제 API 주문 시도(placed/error) 카운터 +1 — 감사로그 truncation과 독립(캡 우회 방지)."""
    with _lock:
        d = _load()
        c = d.setdefault(sub, {}).setdefault("counters", {})
        c[day] = int(c.get(day, 0)) + 1
        for k in sorted(c)[:-10]:      # 최근 10일만 보존
            del c[k]
        _save(d)
        return c[day]


def daily_order_count(sub, day):
    """당일(day='YYYY-MM-DD') 실제 API 주문 시도 수 — 전용 카운터(감사로그 파생 아님)."""
    with _lock:
        return int(_load().get(sub, {}).get("counters", {}).get(day, 0))
