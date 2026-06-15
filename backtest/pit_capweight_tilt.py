"""R5 — 캡가중 인덱스 복제 + 모멘텀 틸트로 인덱스 초과 가능성 검정 (PIT, 생존편향-free).

전략이 KOSPI 매수후보유(+257%)에 진 건 등가중·과소투자의 베타갭 탓. 그럼 ★시총가중(베타 확보)에서
출발해 모멘텀으로 틸트★하면 인덱스를 이기나? 를 정면 검정.
  유니버스: 시점기준 시총 top-N (대형주, 인덱스 근사)
  가중   : w ∝ mktcap × (1 + k·(2·momrank−1)).  k=0 → 순수 시총가중(복제), k>0 → 모멘텀 틸트
  집행   : 월(step≈20거래일) 리밸런스, 종가→종가, 턴오버에만 왕복비용. 상폐=해당 비중 전손(-100%).
벤치: KOSPI 매수후보유(동일 리밸런스 그리드). 과최적 가드=전·후반 분할.
사용: python -m backtest.pit_capweight_tilt [--top 200] [--step 20] [--mom 120]
"""
from __future__ import annotations
import argparse, math, os, sys
import numpy as np, pandas as pd, yaml
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402


def stats(eq, freq=12):
    eq = eq.dropna()
    if len(eq) < 3 or eq.iloc[0] <= 0:
        return None
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    tot = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1 if eq.iloc[-1] > 0 else -1.0
    dd = float((eq / eq.cummax() - 1).min())
    r = eq.pct_change().dropna()
    sh = r.mean() / r.std() * math.sqrt(freq) if r.std() > 0 else 0.0
    return tot, cagr, dd, sh


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=20); ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--mom", type=int, default=120); ap.add_argument("--frm", default="20170501")
    a = ap.parse_args()
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    cost = cfg["cost"]["assumed_round_trip_cost"]; mc = cfg["universe"]["min_close"]
    daily = load_adjusted()
    index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    cal = pd.DatetimeIndex(sorted(daily["date"].unique())); n = len(cal)
    cls = daily.pivot(index="date", columns="ticker", values="close").reindex(cal).where(lambda x: x > 0)
    mcap = daily.pivot(index="date", columns="ticker", values="mktcap").reindex(cal)
    mom = cls / cls.shift(a.mom) - 1
    mrank = mcap.rank(axis=1, ascending=False)
    kospi = index[index["market"] == "KOSPI"].sort_values("date").set_index("date")["close"].reindex(cal).ffill()
    frm = pd.Timestamp(a.frm)
    rebal = [i for i in range(0, n - a.step - 1, a.step) if cal[i] >= frm]
    print(f"리밸런스 {len(rebal)}회({cal[rebal[0]].date()}~{cal[rebal[-1]].date()}, step{a.step}) · 시총top{a.top} · 모멘텀{a.mom}", file=sys.stderr)

    def run(k):
        prevw = {}; nav = 1.0; eq = [(cal[rebal[0]], 1.0)]; turn = 0.0
        for ci in range(len(rebal) - 1):
            i, j = rebal[ci], rebal[ci + 1]
            ii = cls.iloc[i]
            uni = (mrank.iloc[i] <= a.top) & (ii >= mc) & mcap.iloc[i].notna() & mom.iloc[i].notna()
            tks = list(cls.columns[uni.values])
            if len(tks) < 30:
                eq.append((cal[j], nav)); continue
            cap = mcap.iloc[i][tks].astype(float); mr = mom.iloc[i][tks].rank(pct=True)
            w = cap * (1 + k * (2 * mr - 1)).clip(lower=0); w = w / w.sum()
            to = sum(abs(w.get(t, 0.0) - prevw.get(t, 0.0)) for t in set(list(w.index) + list(prevw.keys())))
            turn += to; nav *= (1 - cost / 2 * to)
            jj = cls.iloc[j]
            r_t = (jj / ii - 1).reindex(w.index)
            r_t[jj.reindex(w.index).isna() & ii.reindex(w.index).notna()] = -1.0   # 상폐 전손
            nav *= (1 + float((w * r_t.fillna(0)).sum()))
            prevw = w.to_dict(); eq.append((cal[j], nav))
        return pd.DataFrame(eq, columns=["date", "eq"]).set_index("date")["eq"], turn / max(len(rebal) - 1, 1)

    mid = None
    kb = kospi.reindex([cal[i] for i in rebal], method="ffill")
    ks = stats(kb / kb.iloc[0])
    print(f"\n=== R5 캡가중+모멘텀틸트 (PIT, 월리밸런스, 시총top{a.top}) ===")
    print(f"{'구성':<22}{'누적':>9}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}{'평균턴오버':>9}  전반/후반")
    for k in (0.0, 0.5, 1.0, 2.0):
        eq, to = run(k)
        s = stats(eq)
        m = eq.index[len(eq) // 2]; h1 = stats(eq.loc[:m]); h2 = stats(eq.loc[m:])
        lab = "복제(k=0,시총가중)" if k == 0 else f"틸트 k={k}"
        hh = f"{h1[1]:+.0%}/{h2[1]:+.0%}" if h1 and h2 else "-"
        print(f"{lab:<22}{s[0]:>+8.0%}{s[1]:>+7.1%}{s[2]:>+7.1%}{s[3]:>8.2f}{to:>8.0%}  {hh}")
    print(f"{'벤치 KOSPI 매수보유':<22}{ks[0]:>+8.0%}{ks[1]:>+7.1%}{ks[2]:>+7.1%}{ks[3]:>8.2f}{'-':>9}")
    print("\n※ 복제(k=0)가 KOSPI를 잘 추종하면 방법 OK. 틸트(k>0)가 복제·KOSPI 둘 다 초과해야 '모멘텀 틸트 알파'.")


if __name__ == "__main__":
    main()
