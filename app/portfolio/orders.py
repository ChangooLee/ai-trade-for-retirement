"""주문 후보 생성·저장 (§33,17). REVIEW는 주문이 아니라 별도 daily_reviews.csv."""
from __future__ import annotations

import json
import os

import pandas as pd

ORDER_COLS = ["asof_date", "action", "ticker", "name", "market", "weight", "reason",
              "entry_or_exit_rule", "next_execution", "reference_price", "notes"]


def build_orders(buys, sells, asof, next_day, weight_per_stock, exposure) -> list:
    orders = []
    for _, r in buys.iterrows():
        orders.append({
            "asof_date": str(asof.date()), "action": "BUY", "ticker": r["ticker"],
            "name": r["name"], "market": r["market"], "weight": round(weight_per_stock, 4),
            "reason": "F_LEADER_20W_PULLBACK", "entry_or_exit_rule": "NEXT_OPEN_ENTRY",
            "next_execution": str(next_day.date()), "reference_price": round(float(r["close"]), 1),
            "notes": f"D4 {exposure['mode']}, RS {r['rs_rank']:.0f}, 52w {r['high_52w_ratio']:.0%}"})
    for _, r in sells.iterrows():
        ret = r.get("ret")
        orders.append({
            "asof_date": str(asof.date()), "action": "SELL", "ticker": r["ticker"],
            "name": r["name"], "market": r["market"],
            "weight": round(float(r.get("entry_weight") or 0), 4),
            "reason": "HOLDING_40D_TIME_EXIT", "entry_or_exit_rule": "NEXT_OPEN_EXIT",
            "next_execution": str(next_day.date()),
            "reference_price": round(float(r["cur"]), 1) if r["cur"] == r["cur"] else None,
            "notes": f"holding_days={r['holding_days']}" + (f", return={ret:.2%}" if ret == ret else "")})
    return orders


def write_orders(orders, reviews_df, out_dir, asof) -> tuple:
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "daily_orders.csv")
    json_path = os.path.join(out_dir, "daily_orders.json")
    pd.DataFrame(orders, columns=ORDER_COLS).to_csv(csv_path, index=False, encoding="utf-8-sig")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2, default=str)
    if reviews_df is not None and len(reviews_df):
        reviews_df.to_csv(os.path.join(out_dir, "daily_reviews.csv"), index=False, encoding="utf-8-sig")
    return csv_path, json_path
