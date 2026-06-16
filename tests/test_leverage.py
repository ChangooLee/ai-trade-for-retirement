"""레버리지 시뮬 엔진 검증 — 무차입 동작 보존 / 2x 차입 / 마진이자 / 마진콜 강제청산."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.sim import engine as E
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock

CAL = [f"2026-01-{d:02d}" for d in range(1, 28)]
CANDS = [{"ticker": f"{i:06d}", "name": f"S{i}", "close": 1000.0} for i in range(1, 16)]


def _sig(m2, prices):
    slots = compute_target_slots(m2, 15, 0.05); w = compute_weight_per_stock(m2, slots)
    return {"hold_days": 40, "cost": 0.0035, "calendar": CAL, "sell_tickers": [], "prices": prices,
            "buy_order": CANDS, "exposure": {"slots": int(slots), "weight": w, "max_lev": round(m2, 4)},
            "margin_rate": 0.07}


def test_no_leverage_never_borrows():
    """m2≤1.0 → 차입 0, 현금 음수 불가 (기존 동작 보존)."""
    px = {c["ticker"]: 1000.0 for c in CANDS}
    ns, r = E.execute_day(E.new_state(10_000_000), "2026-01-02", _sig(1.0, px))
    assert r["borrowed"] == 0
    assert r["cash"] >= 0
    assert r["leverage"] <= 1.0


def test_2x_borrows_about_100pct():
    """m2=2.0 → ~100% 차입, 레버리지 ~1.8~2.0x."""
    px = {c["ticker"]: 1000.0 for c in CANDS}
    ns, r = E.execute_day(E.new_state(10_000_000), "2026-01-02", _sig(2.0, px))
    assert r["borrowed"] > 7_000_000          # 자기자본 ~100% 차입
    assert r["cash"] < 0
    assert 1.7 <= r["leverage"] <= 2.0


def test_margin_interest_accrues():
    """차입잔액에 일할 이자 부과(연7%/252)."""
    px = {c["ticker"]: 1000.0 for c in CANDS}
    ns, _ = E.execute_day(E.new_state(10_000_000), "2026-01-02", _sig(2.0, px))
    _, r2 = E.execute_day(ns, "2026-01-03", _sig(2.0, px))
    assert r2["margin_interest"] > 0
    assert abs(r2["margin_interest"] - (-ns["cash"]) * 0.07 / 252) < 50   # 근사 일치


def test_margin_call_liquidates_on_crash():
    """2x 보유 중 보유가 큰 폭 하락 → 유지증거금 미달 시 전량 강제청산."""
    up = {c["ticker"]: 1000.0 for c in CANDS}
    ns, _ = E.execute_day(E.new_state(10_000_000), "2026-01-02", _sig(2.0, up))
    crash = {tk: 600.0 for tk in up}          # -40%
    _, r = E.execute_day(ns, "2026-01-06", _sig(2.0, crash))
    assert r["margin_called"] is True
    assert r["n_positions"] == 0              # 전량 청산
    # -30%는 1.87x에선 유지증거금(0.30) 상회 → 미발동(생존)
    mild = {tk: 700.0 for tk in up}
    _, r2 = E.execute_day(ns, "2026-01-06", _sig(2.0, mild))
    assert r2["margin_called"] is False
