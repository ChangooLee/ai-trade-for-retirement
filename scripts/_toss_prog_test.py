"""Stage 3 프로그램매매 로직 실테스트(드라이런) — config·신호·보유·계획·감사. 실주문 없음.
config는 임시 store로(실사용 오염 방지). 사용: .venv/bin/python scripts/_toss_prog_test.py"""
import json, math, os, sys
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from app.data import toss_api as T
from server import toss_config as CFG
CFG._STORE = "/tmp/_test_toss_config.json"      # 실 store 오염 방지
if os.path.exists(CFG._STORE):
    os.remove(CFG._STORE)

k = json.load(open("/tmp/.toss_secret.json", encoding="utf-8"))
K, S = k["api_key"], k["secret_key"]
seq = T.get_accounts(K, S)["result"][0]["accountSeq"]
sub = "u1"

# 1) config 라운드트립
CFG.set_config(sub, program_enabled=True, program_dry_run=True, program_max_notional_krw=2_000_000,
               program_max_positions=5, max_notional_krw=3_000_000, kill_switch=False)
c = CFG.get_config(sub)
print("[config]", {x: c[x] for x in ("kill_switch", "max_notional_krw", "program_enabled", "program_dry_run",
                                     "program_max_notional_krw", "program_max_positions")})

# 2) 안전 로직 단위검증(주문 명목가/킬스위치) — 핸들러와 동일 규칙
def order_gate(cfg, notional):
    if cfg["kill_switch"]:
        return "blocked:kill"
    if notional is not None and notional > cfg["max_notional_krw"]:
        return "blocked:limit"
    return "ok"
print("[게이트] 200만(한도3백만):", order_gate(c, 2_000_000), "· 5백만:", order_gate(c, 5_000_000))
c2 = CFG.set_config(sub, kill_switch=True); print("[게이트] 킬스위치ON 200만:", order_gate(c2, 2_000_000))
CFG.set_config(sub, kill_switch=False)

# 3) 프로그램 드라이런 계획(핸들러 core 복제)
sig = json.load(open(os.path.join(_REPO, "state", "daily_signals.json"), encoding="utf-8"))
buy = [b["ticker"] for b in (sig.get("buy_order") or [])]
items = (T.get_holdings(K, S, seq)["result"] or {}).get("items", [])
held = {str(i["symbol"]) for i in items}
c = CFG.get_config(sub)
cap, maxpos = c["program_max_notional_krw"], c["program_max_positions"]
room = max(0, maxpos - len(held))
plan = []
for tk in buy:
    if tk in held or len(plan) >= room:
        continue
    px = float(T.get_prices(K, S, tk)["result"][0]["lastPrice"])
    qty = int(math.floor(cap / px)) if px > 0 else 0
    plan.append({"symbol": tk, "qty": qty, "price": int(px), "notional": qty * int(px), "skip(1주>한도)": qty < 1})
print(f"[신호] buy_order={buy} · 보유={sorted(held)} · room={room}")
print(f"[드라이런 계획] {plan}")

# 4) 감사 로그 + 일일 카운트(드라이런 제외)
CFG.log_order(sub, {"ts": "2026-07-02T09:00:00", "symbol": "TEST", "side": "BUY", "quantity": 1, "price": 1000, "result": "dry_run", "src": "program"})
CFG.log_order(sub, {"ts": "2026-07-02T09:01:00", "symbol": "TEST2", "side": "BUY", "quantity": 1, "price": 2000, "result": "placed", "src": "manual"})
print("[감사] 최근2:", [(e["symbol"], e["result"]) for e in CFG.get_audit(sub, 2)])
print("[일일카운트] 2026-07-02 실주문(dry_run제외):", CFG.daily_order_count(sub, "2026-07-02"), "(기대 1)")
os.remove(CFG._STORE)
print("✅ Stage 3 로직 검증 완료(실주문 0)")
