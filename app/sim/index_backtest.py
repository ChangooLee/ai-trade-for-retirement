"""인덱스 로테이션 백테스트 — KOSPI/KOSDAQ 40주선 추세추종 + 13주 상대강도 스타일 로테이션.
검증(PIT 생존편향제거 2017~2026): 이 레짐선 인덱스 추세추종/로테이션이 active 종목선택을 압도(Sharpe 0.80~0.84).
mode: rotation(둘 중 강한 쪽·둘 다 아래면 현금) / kospi_tf(KOSPI 추세추종) / hold(KOSPI 보유).
출력(stdout JSON): {summary, trades(로테이션 구간), equity_curve}
사용: python -m app.sim.index_backtest --start YYYY-MM-DD --end YYYY-MM-DD [--capital N] [--mode rotation]
"""
from __future__ import annotations
import argparse, json, math, os, sys
import pandas as pd
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
import yaml  # noqa: E402
NM = {"ko": "KOSPI", "kq": "KOSDAQ"}


def run(start, end, capital, mode="rotation"):
    cfg = yaml.safe_load(open(os.path.join(_REPO, "config/strategy.yaml"), encoding="utf-8"))
    idx = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    def ser(mk):
        return idx[idx["market"] == mk].sort_values("date").set_index("date")["close"].astype(float)
    ko, kq = ser("KOSPI"), ser("KOSDAQ")
    ma_ko, ma_kq = ko.rolling(40).mean(), kq.rolling(40).mean()
    mom_ko, mom_kq = ko.pct_change(13), kq.pct_change(13)
    df = pd.concat([ko, kq, ma_ko, ma_kq, mom_ko, mom_kq], axis=1).dropna()
    df.columns = ["ko", "kq", "mko", "mkq", "rko", "rkq"]
    df = df[(df.index >= start) & (df.index <= end)].sort_index()
    if len(df) < 10:
        return {"error": "해당 기간 인덱스 데이터 부족(주봉 10개 미만)"}
    eq = float(capital); eqc = []; legs = []; cur = None; ls = None; lseq = None; peak = eq; mdd = 0.0
    for i in range(len(df)):
        dstr = str(df.index[i].date())
        if i > 0 and cur in ("ko", "kq"):
            eq *= df[cur].iloc[i] / df[cur].iloc[i - 1]
        eqc.append({"date": dstr, "equity": round(eq)})
        peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
        if mode == "hold":
            pick = "ko"
        elif mode == "kospi_tf":
            pick = "ko" if df["ko"].iloc[i] > df["mko"].iloc[i] else None
        else:
            koi = df["ko"].iloc[i] > df["mko"].iloc[i]; kqi = df["kq"].iloc[i] > df["mkq"].iloc[i]
            pick = ("ko" if df["rko"].iloc[i] >= df["rkq"].iloc[i] else "kq") if (koi and kqi) else ("ko" if koi else ("kq" if kqi else None))
        if pick != cur:
            if cur in ("ko", "kq") and ls is not None:
                legs.append({"ticker": NM[cur], "name": NM[cur], "entry_date": ls, "exit_date": dstr,
                             "ret": eq / lseq - 1, "reason": "추세/스타일 전환"})
            cur = pick
            ls, lseq = (dstr, eq) if cur in ("ko", "kq") else (None, None)
    if cur in ("ko", "kq") and ls is not None:
        legs.append({"ticker": NM[cur], "name": NM[cur], "entry_date": ls, "exit_date": str(df.index[-1].date()),
                     "ret": eq / lseq - 1, "reason": "보유중"})
    r = pd.Series([e["equity"] for e in eqc], dtype=float).pct_change().dropna()
    yrs = (df.index[-1] - df.index[0]).days / 365.25
    cagr = (eq / capital) ** (1 / yrs) - 1 if yrs > 0 and eq > 0 else 0.0
    shp = r.mean() / r.std() * math.sqrt(52) if r.std() > 0 else 0.0
    wins = sum(1 for lg in legs if lg["ret"] > 0)
    cash_wk = int((pd.Series([1] * len(df))).sum() - sum(1 for e in eqc))  # 0 (참고)
    return {
        "summary": {"start": str(df.index[0].date()), "end": str(df.index[-1].date()), "days": len(df),
                    "capital": capital, "final_equity": round(eq), "total_pnl": round(eq - capital),
                    "total_ret": eq / capital - 1, "mdd": mdd, "cagr": cagr, "sharpe": round(shp, 2),
                    "n_trades": len(legs), "win_rate": (wins / len(legs)) if legs else 0.0,
                    "strategy": "index", "mode": mode},
        "trades": legs[::-1][:200], "equity_curve": eqc,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=float, default=10_000_000)
    ap.add_argument("--mode", default="rotation", choices=["rotation", "kospi_tf", "hold"])
    a = ap.parse_args()
    try:
        out = run(a.start, a.end, a.capital, a.mode)
    except Exception as e:
        out = {"error": str(e)}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
