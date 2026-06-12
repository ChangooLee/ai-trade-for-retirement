"""목표 비중 산출 (§30) — 노출÷목표슬롯, 합산 100%."""
from __future__ import annotations

import math


def compute_target_slots(exposure: float, max_positions: int, base_slot_weight: float) -> int:
    if exposure <= 0:
        return 0
    return min(max_positions, math.ceil(exposure / base_slot_weight))


def compute_weight_per_stock(exposure: float, target_slots: int) -> float:
    return exposure / target_slots if target_slots > 0 else 0.0


def compute_buy_count(target_slots: int, open_after_sells: int, n_candidates: int) -> int:
    return max(0, min(target_slots - open_after_sells, n_candidates))
