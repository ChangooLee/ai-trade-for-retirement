"""코어-위성 백테스트 — 코어=인덱스 로테이션(index_backtest) + 위성=active 모멘텀(backtest_cli).
주간 리밸런스로 코어비중 w 블렌드. 사용자 결정: 코어 70% / 위성 30%. 순수 코어·순수 active·KOSPI 대비 검증.
사용: python -m backtest.core_satellite_backtest [--start ..] [--end ..]
"""
from __future__ import annotations
import argparse, json, math, os, sys
import pandas as pd
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, _REPO)
from app.sim import index_backtest, backtest_cli  # noqa: E402
from app.data import krx_loader as L  # noqa: E402
import yaml  # noqa: E402


def eqser(out):
    ec = out.get("equity_curve") or []
    if not ec:
        return None
    return pd.Series([e["equity"] for e in ec], index=pd.to_datetime([e["date"] for e in ec])).sort_index()


def metr(eq):
    eq = eq / eq.iloc[0]; yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    dd = float((eq / eq.cummax() - 1).min()); r = eq.pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(52) if r.std() > 0 else 0
    return eq.iloc[-1] - 1, cagr, dd, shp


def run(start, end, capital, core_weight=0.7):
    """코어-위성 블렌드 백테스트(주간 리밸런스). 코어=인덱스 로테이션, 위성=active 스윙.
    반환(백테스트 응답 형식): {summary, trades(코어 로테이션 구간), equity_curve}."""
    w = min(max(float(core_weight), 0.0), 1.0)
    core = index_backtest.run(start, end, capital, "rotation")
    act = backtest_cli.run(start, end, capital, 1.0, 0.03, "block")
    for o in (core, act):
        if isinstance(o, dict) and o.get("error"):
            return o
    cs, as_ = eqser(core), eqser(act)
    if cs is None or as_ is None:
        return {"error": "해당 기간 데이터 부족"}
    cw = cs.resample("W").last().ffill(); aw = as_.resample("W").last().ffill()
    df = pd.concat([cw, aw], axis=1).ffill().dropna(); df.columns = ["core", "act"]
    if len(df) < 3:
        return {"error": "기간이 너무 짧습니다(주간 3개 미만)"}
    cr = df["core"].pct_change().fillna(0); ar = df["act"].pct_change().fillna(0)
    blend = (1 + (w * cr + (1 - w) * ar)).cumprod() * float(capital)
    yrs = (blend.index[-1] - blend.index[0]).days / 365.25
    cagr = (blend.iloc[-1] / capital) ** (1 / yrs) - 1 if yrs > 0 else 0.0
    mdd = float((blend / blend.cummax() - 1).min())
    r = blend.pct_change().dropna(); shp = r.mean() / r.std() * math.sqrt(52) if r.std() > 0 else 0.0
    eqc = [{"date": str(ix.date()), "equity": round(v)} for ix, v in blend.items()]
    legs = (core.get("trades") or [])
    return {
        "summary": {"start": eqc[0]["date"], "end": eqc[-1]["date"], "days": len(df),
                    "capital": float(capital), "final_equity": round(blend.iloc[-1]),
                    "total_pnl": round(blend.iloc[-1] - capital), "total_ret": blend.iloc[-1] / capital - 1,
                    "mdd": mdd, "cagr": cagr, "sharpe": round(shp, 2),
                    "n_trades": len(legs), "win_rate": core.get("summary", {}).get("win_rate", 0.0),
                    "strategy": "coresat", "core_weight": w,
                    "note": f"코어(지수 로테이션) {w:.0%} + 위성(active 발굴) {1 - w:.0%} · 주간 리밸런스"},
        "trades": legs, "equity_curve": eqc,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-05-01"); ap.add_argument("--end", default="2026-07-01")
    a = ap.parse_args(); cap = 10_000_000
    core = index_backtest.run(a.start, a.end, cap, "rotation")
    act = backtest_cli.run(a.start, a.end, cap, 1.0, 0.03, "block")
    cs, as_ = eqser(core), eqser(act)
    if cs is None or as_ is None:
        print("데이터 부족"); return
    cw = cs.resample("W").last().ffill(); aw = as_.resample("W").last().ffill()
    df = pd.concat([cw, aw], axis=1).ffill().dropna(); df.columns = ["core", "act"]
    cr = df["core"].pct_change().fillna(0); ar = df["act"].pct_change().fillna(0)
    # KOSPI 벤치
    idx = L.load_index_ohlcv(yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))["paths"]["index_ohlcv"])
    ko = idx[idx["market"] == "KOSPI"].sort_values("date").set_index("date")["close"].astype(float)
    ko = ko[(ko.index >= df.index[0]) & (ko.index <= df.index[-1])]
    print(f"\n=== 코어-위성 백테스트 ({df.index[0].date()}~{df.index[-1].date()} · 주간 리밸런스) ===")
    print(f"  {'구성':22s} {'총':>8s} {'CAGR':>7s} {'MDD':>7s} {'Sharpe':>7s}")
    for w, lbl in [(1.0, "코어 100%(순수 인덱스)"), (0.8, "코어 80/위성 20"), (0.7, "코어 70/위성 30 ★"),
                   (0.5, "50/50"), (0.0, "위성 100%(순수 active)")]:
        blend = (1 + (w * cr + (1 - w) * ar)).cumprod()
        t, c, d, s = metr(blend)
        print(f"  {lbl:22s} {t:+8.0%} {c:+7.1%} {d:+7.0%} {s:7.2f}")
    if len(ko) > 2:
        t, c, d, s = metr(ko); print(f"  {'(참고) KOSPI 보유':22s} {t:+8.0%} {c:+7.1%} {d:+7.0%} {s:7.2f}")


if __name__ == "__main__":
    if "--json" in sys.argv:      # API용: JSON 한 줄 출력(sync_api가 마지막 줄 파싱)
        ap = argparse.ArgumentParser()
        ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
        ap.add_argument("--capital", type=float, default=10_000_000)
        ap.add_argument("--core-weight", type=float, default=0.7)
        ap.add_argument("--json", action="store_true")
        a = ap.parse_args()
        print(json.dumps(run(a.start, a.end, a.capital, a.core_weight), ensure_ascii=False))
    else:
        main()
