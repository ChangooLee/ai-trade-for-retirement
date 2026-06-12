"""§21.3 수치 검증 — 목표슬롯/비중이 노출에 맞게 산출되는가."""
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock, compute_buy_count


def test_target_slots():
    assert compute_target_slots(0.40, 15, 0.05) == 8
    assert compute_target_slots(0.70, 15, 0.05) == 14
    assert compute_target_slots(1.00, 15, 0.05) == 15   # min(15, 20)
    assert compute_target_slots(0.0, 15, 0.05) == 0


def test_weight_per_stock():
    assert round(compute_weight_per_stock(0.40, 8), 4) == 0.05
    assert round(compute_weight_per_stock(0.70, 14), 4) == 0.05
    assert round(compute_weight_per_stock(1.00, 15), 4) == 0.0667
    assert compute_weight_per_stock(0.4, 0) == 0.0


def test_invested_sums_to_exposure():
    # 동일가중 × 슬롯수 = 노출 (투자+현금=100% 보장)
    for expo in (0.4, 0.7, 1.0):
        slots = compute_target_slots(expo, 15, 0.05)
        w = compute_weight_per_stock(expo, slots)
        assert abs(w * slots - expo) < 1e-9


def test_buy_count():
    assert compute_buy_count(8, 6, 5) == 2     # 슬롯8 - 보유6 = 2, 후보5 → 2
    assert compute_buy_count(8, 6, 1) == 1     # 후보 부족 → 1
    assert compute_buy_count(8, 10, 5) == 0    # 초과보유 → 신규 0
