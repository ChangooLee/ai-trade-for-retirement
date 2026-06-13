"""기간 백테스트 CLI — 아카이브(bt_days.json + bt_prices.parquet)에 시뮬 엔진을 재생.

API(/api/backtest)가 subprocess로 호출해 결과 JSON을 받는다(메모리 격리·venv 자유). 빠름(엔진 스텝만).
입력: --start --end --capital --exposure-mult --cb-limit
출력(stdout JSON): {summary{...}, trades[...], equity_curve[...]}
"""
from __future__ import annotations
import argparse, json, os, sys
import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
from app.sim import engine  # noqa: E402
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock  # noqa: E402

DAYS_PATH = os.path.join(_REPO, "state", "bt_days.json")
PRICES_PATH = os.path.join(_REPO, "state", "bt_prices.parquet")


def run(start, end, capital, exposure_mult, cb_limit, cb_mode="block"):
    arch = json.load(open(DAYS_PATH, encoding="utf-8"))
    sizing = arch.get("sizing", {})
    maxpos = int(sizing.get("max_positions", 15)); baseslot = float(sizing.get("base_slot_weight", 0.05))
    hold_days = int(sizing.get("hold_days", 40)); cost = float(sizing.get("cost", 0.0035))
    names = arch.get("names", {}); calendar = arch.get("calendar", [])
    px = pd.read_parquet(PRICES_PATH)
    px = px[(px["date"] >= start) & (px["date"] <= end)]
    prices_by_date = {d: dict(zip(g["ticker"], g["close"])) for d, g in px.groupby("date")}
    days = sorted(d for d in arch["days"].keys() if start <= d <= end)
    if not days:
        return {"error": "해당 기간에 데이터가 없습니다."}
    st = engine.new_state(capital, cb_limit=cb_limit, cb_mode=cb_mode)
    eq_curve = []; trips = 0; all_trades = []
    for d in days:
        rec = arch["days"][d]; pr = prices_by_date.get(d, {})
        m = min(1.0, rec["m"] * exposure_mult)
        slots = compute_target_slots(m, maxpos, baseslot); weight = compute_weight_per_stock(m, slots)
        buy = [{"ticker": tk, "name": names.get(tk, tk), "close": pr[tk]} for tk in rec["buy"] if tk in pr and pr[tk] > 0]
        sig = {"hold_days": hold_days, "cost": cost, "exposure": {"slots": int(slots), "weight": weight},
               "buy_order": buy, "sell_tickers": rec["sells"], "prices": pr, "calendar": calendar}
        st, res = engine.execute_day(st, d, sig)
        if res["tripped"]:
            trips += 1
        all_trades.extend(res["trades"])
        eq_curve.append({"date": d, "equity": res["equity"]})
    eqs = pd.Series([e["equity"] for e in eq_curve])
    final = float(eqs.iloc[-1]); mdd = float((eqs / eqs.cummax() - 1).min())
    wins = sum(1 for t in all_trades if t["pnl"] > 0)
    realized = sum(t["pnl"] for t in all_trades)
    open_pos = st["positions"]
    return {
        "summary": {"start": days[0], "end": days[-1], "days": len(days), "capital": capital,
                    "exposure_mult": exposure_mult, "cb_limit": cb_limit, "cb_mode": cb_mode,
                    "final_equity": round(final), "total_pnl": round(final - capital),
                    "total_ret": final / capital - 1, "mdd": mdd, "trips": trips,
                    "n_trades": len(all_trades), "win_rate": (wins / len(all_trades)) if all_trades else 0.0,
                    "realized_pnl": round(realized), "n_open": len(open_pos), "cash": round(st["cash"])},
        "trades": sorted(all_trades, key=lambda t: t["exit_date"], reverse=True)[:200],
        "equity_curve": eq_curve,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=float, default=10_000_000)
    ap.add_argument("--exposure-mult", type=float, default=1.0)
    ap.add_argument("--cb-limit", type=float, default=0.03)
    ap.add_argument("--cb-mode", default="block", choices=["block", "liq", "liqsoft"])
    a = ap.parse_args()
    try:
        out = run(a.start, a.end, a.capital, max(0.5, min(2.5, a.exposure_mult)), max(0.0, min(0.20, a.cb_limit)), a.cb_mode)
    except FileNotFoundError:
        out = {"error": "아카이브가 아직 생성되지 않았습니다(build_bt_archive 필요)."}
    except Exception as e:
        out = {"error": str(e)}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
