"""코어-위성 배포 검증(서버 실행): asof 계산 → 재빌드 → core_alloc 확인 → 임시 db 엔드투엔드 시뮬.
사용: .venv/bin/python scripts/_coresat_validate.py
"""
import json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
from app.data import krx_loader as L
from app.sim import db, engine

cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
asof = str(L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])["date"].max().date()).replace("-", "")
print(f"[1] 재빌드 asof={asof}")
r = subprocess.run([sys.executable, "-m", "app.batch.build_webapp", "--asof", asof,
                    "--out", "/var/www/leaders/index.html"], capture_output=True, text=True)
line = [x for x in (r.stdout + r.stderr).splitlines() if "생성" in x or "Traceback" in x]
print("   " + (line[-1] if line else "(빌드 출력 없음)"))

sig = json.load(open("state/daily_signals.json"))
print(f"[2] core_alloc = {sig.get('core_alloc')}")

print("[3] 코어-위성 시뮬 엔드투엔드(임시 db)")
P = "/tmp/test_coresat.db"
if os.path.exists(P):
    os.remove(P)
db.init(P)
db.start_sim("t", "t@t.com", 10_000_000, "2026-01-01", path=P, core_weight=0.7)
s = db.get_sim("t", P)
state = db.state_from_row(s)
ns, res1 = engine.execute_day_coresat(state, sig["asof"], sig)
db.save_step("t", ns, res1, path=P)
res = db.results("t", P)
n_core = sum(1 for p in res["positions"] if p.get("core"))
print(f"   core_weight={res['core_weight']} equity={res['equity']:,} cash={res['cash']:,} 위성={res['n_positions'] - n_core}종목")
print(f"   core_index={res1['core_index']} core_value={res1['core_value']:,} core_pct={res1['core_pct']:.0%} sat_value={res1['sat_value']:,}")
for p in res["positions"]:
    tag = "[코어]" if p.get("core") else "[위성]"
    print(f"     {tag} {p.get('ticker')} {p.get('name')} shares={round(p['shares'], 2)} entry={p['entry_price']}")
os.remove(P)
print("[4] 검증 완료")
