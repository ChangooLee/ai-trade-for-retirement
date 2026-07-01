"""execute_day_coresat 단독 검증 — 코어 70% 배분·로테이션 전환·추세이탈 현금화·위성 청산."""
import os, sys
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, _REPO)
from app.sim import engine  # noqa: E402

CAL = [f"2026-07-{d:02d}" for d in range(1, 21)]


def sig(day, lead, kospi, kosdaq, prices, buy=None):
    return {"asof": day, "cost": 0.0035, "hold_days": 40, "calendar": CAL,
            "exposure": {"slots": 4, "weight": 0.10, "mode": "D4"},
            "buy_order": buy or [], "sell_tickers": [], "prices": prices,
            "core_alloc": {"lead": lead, "kospi": kospi, "kosdaq": kosdaq}}


st = {"investment": 10_000_000, "cash": 10_000_000, "positions": [], "core_weight": 0.7}
buy = [{"ticker": "A", "name": "가", "close": 10000}, {"ticker": "B", "name": "나", "close": 20000}]

# Day1: KOSPI 리드 → 코어 70% + 위성 A/B
st, r = engine.execute_day_coresat(st, "2026-07-01", sig("2026-07-01", "KOSPI", 3000, 1000, {"A": 10000, "B": 20000}, buy))
print(f"D1 KOSPI리드: eq={r['equity']:,} 코어={r['core_value']:,}({r['core_pct']:.0%},{r['core_index']}) 위성={r['sat_value']:,}({r['n_positions']}종목) 현금={r['cash']:,}")
assert 0.68 <= r["core_pct"] <= 0.72, f"코어비중 {r['core_pct']}"
assert r["n_positions"] == 2 and r["core_index"] == "KOSPI"

# Day2: KOSPI +10% → 코어 가치↑, 드리프트 작음(리밸 안함)
st, r = engine.execute_day_coresat(st, "2026-07-02", sig("2026-07-02", "KOSPI", 3300, 1000, {"A": 10500, "B": 20000}, buy))
print(f"D2 KOSPI+10%: eq={r['equity']:,} 코어={r['core_value']:,}({r['core_pct']:.0%}) 위성={r['sat_value']:,} trades={len(r['trades'])}")
assert r["core_index"] == "KOSPI"

# Day3: 리드 KOSDAQ 전환 → 코어 로테이션(KOSPI 청산→KOSDAQ 매수)
st, r = engine.execute_day_coresat(st, "2026-07-03", sig("2026-07-03", "KOSDAQ", 3300, 1100, {"A": 10500, "B": 20000}, buy))
print(f"D3 KOSDAQ전환: eq={r['equity']:,} 코어={r['core_value']:,}({r['core_pct']:.0%},{r['core_index']}) trades={[t['reason'] for t in r['trades']]}")
assert r["core_index"] == "KOSDAQ", f"전환 실패 {r['core_index']}"
assert any("전환" in t["reason"] for t in r["trades"])

# Day4: 두 지수 추세 아래(lead=None) → 코어 청산→현금
st, r = engine.execute_day_coresat(st, "2026-07-04", sig("2026-07-04", None, 3300, 1100, {"A": 10500, "B": 20000}, buy))
print(f"D4 추세이탈: eq={r['equity']:,} 코어={r['core_value']:,}({r['core_pct']:.0%}) 현금={r['cash']:,} trades={[t['reason'] for t in r['trades']]}")
assert r["core_value"] == 0 and r["core_index"] is None, "코어 현금화 실패"

# Day5: KOSPI 복귀 → 코어 재매수
st, r = engine.execute_day_coresat(st, "2026-07-05", sig("2026-07-05", "KOSPI", 3300, 1100, {"A": 10500, "B": 20000}, buy))
print(f"D5 KOSPI복귀: eq={r['equity']:,} 코어={r['core_value']:,}({r['core_pct']:.0%},{r['core_index']})")
assert r["core_index"] == "KOSPI" and 0.68 <= r["core_pct"] <= 0.72

print("\n✅ 코어-위성 엔진 전 케이스 통과 (배분·전환·현금화·복귀)")
