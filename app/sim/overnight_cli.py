"""오버나이트 단타 기간 백테스트 CLI — 아카이브(overnight_days.json) 재생.

API(/api/backtest?strategy=overnight)가 subprocess로 호출. 신호 top-K 동일가중 1박(종가매수→익일시가매도),
노출 비중(기본 30%)만큼 자본 투입, 왕복비용 0.35%/박. Risk-Off 밤 게이트(기본 끔: 검증상 그 밤이 수익원).
입력: --start --end --capital --exposure(0~1) --topk --gate(on|off)
출력(stdout JSON): {summary{...}, trades[...], equity_curve[...]}
"""
from __future__ import annotations
import argparse, json, math, os, sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARCH = os.path.join(_REPO, "state", "overnight_days.json")
COST = 0.0035


def run(start, end, capital, exposure, topk, gate):
    arch = json.load(open(ARCH, encoding="utf-8"))
    days = sorted(d for d in arch["days"].keys() if start <= d <= end)
    if not days:
        return {"error": "해당 기간에 단타 신호 데이터가 없습니다."}
    eq = float(capital); curve = []; nightly = []; trades = []; n_trade_nights = 0
    for d in days:
        rec = arch["days"][d]; sigs = rec.get("sigs", [])[:topk]
        skip = gate and rec.get("m") == "Risk-Off"
        if sigs and not skip:
            night = sum(s["r"] for s in sigs) / len(sigs)          # 동일가중 1박 수익
            port_ret = exposure * (night - COST)                   # 노출 비중만 투입, 왕복비용
            n_trade_nights += 1
            for s in sigs:
                trades.append({"ticker": s["t"], "name": s["n"], "entry_date": d, "exit_date": d,
                               "ret": s["r"] - COST, "pnl": round(exposure / max(1, len(sigs)) * eq * (s["r"] - COST))})
        else:
            port_ret = 0.0
        eq *= (1 + port_ret); nightly.append(port_ret)
        curve.append({"date": d, "equity": round(eq)})
    arr = [c["equity"] for c in curve]
    final = arr[-1]; peak = arr[0]; mdd = 0.0
    for v in arr:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    import statistics as st
    sd = st.pstdev(nightly) if len(nightly) > 1 else 0
    shp = (sum(nightly) / len(nightly)) / sd * math.sqrt(252) if sd > 0 else 0.0
    wins = sum(1 for t in trades if t["ret"] > 0)
    return {
        "summary": {"start": days[0], "end": days[-1], "days": len(days), "capital": capital,
                    "strategy": "overnight", "exposure": exposure, "topk": topk, "gate": gate,
                    "final_equity": round(final), "total_pnl": round(final - capital),
                    "total_ret": final / capital - 1, "mdd": float(mdd),
                    "n_trades": len(trades), "trade_nights": n_trade_nights,
                    "win_rate": (wins / len(trades)) if trades else 0.0,
                    "exposure_mult": "—", "cb_limit": 0},
        "trades": sorted(trades, key=lambda t: (t["exit_date"], -abs(t["pnl"])), reverse=True)[:200],
        "equity_curve": curve,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=float, default=10_000_000)
    ap.add_argument("--exposure", type=float, default=0.3)
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--gate", default="off", choices=["on", "off"])
    a = ap.parse_args()
    try:
        out = run(a.start, a.end, a.capital, max(0.0, min(1.0, a.exposure)), max(1, min(8, a.topk)), a.gate == "on")
    except FileNotFoundError:
        out = {"error": "단타 아카이브가 아직 생성되지 않았습니다(build_overnight_archive 필요)."}
    except Exception as e:
        out = {"error": str(e)}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
