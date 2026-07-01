"""팩터틸트 롱온리 백테스트 (PIT 생존편향제거) — 현 regime 승자 팩터로 종목선택.
저변동+대형+단기반전+52주고점 복합 z-score → 상위 N 등가중, 월 리밸런스, 익일 시가 집행, 왕복비용.
관건: 이 틸트가 KOSPI 베타 복제(=그냥 인덱스)인가 초과수익인가. 저변동만/저변동+대형/4팩터 비교.
사용: python -m backtest.factor_tilt_backtest
"""
from __future__ import annotations
import math, os, sys
import numpy as np, pandas as pd, yaml
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, _REPO)
from backtest.early_cut_diagnostic import load_adjusted  # noqa: E402


def main():
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    cost = cfg["cost"]["assumed_round_trip_cost"]; cap0 = cfg["portfolio"]["initial_capital"]
    d = load_adjusted().sort_values(["ticker", "date"])
    cl = d.pivot_table(index="date", columns="ticker", values="close")
    op = d.pivot_table(index="date", columns="ticker", values="open")
    mc = d.pivot_table(index="date", columns="ticker", values="mktcap")
    tv = d.pivot_table(index="date", columns="ticker", values="trdval")
    clf = cl.ffill()
    cal = list(cl.index); n = len(cal); ret = cl.pct_change()
    F = {"lowvol": -ret.rolling(60, min_periods=40).std(), "size": np.log(mc.where(mc > 0)),
         "rev": -(cl / cl.shift(21) - 1), "hi52": cl / cl.rolling(252, min_periods=120).max()}
    _fp = os.path.join(_REPO, "data/cache/fundamental_v1.parquet")   # 가치/퀄리티
    if os.path.exists(_fp):
        fun = pd.read_parquet(_fp); fun["date"] = pd.to_datetime(fun["date"])
        _per = fun.pivot_table(index="date", columns="ticker", values="per").reindex(cl.index, method="ffill")
        _pbr = fun.pivot_table(index="date", columns="ticker", values="pbr").reindex(cl.index, method="ffill")
        _eps = fun.pivot_table(index="date", columns="ticker", values="eps").reindex(cl.index, method="ffill")
        _bps = fun.pivot_table(index="date", columns="ticker", values="bps").reindex(cl.index, method="ffill")
        F["val_ep"] = 1.0 / _per.where(_per > 0)
        F["val_bp"] = 1.0 / _pbr.where(_pbr > 0)
        F["qual"] = (_eps / _bps).where(_bps > 0)
    avgtv = tv.rolling(20, min_periods=10).mean()
    N = 20; STEP = 20; WARM = 252

    def zc(s):
        sd = s.std()
        return (s - s.mean()) / sd if sd and sd > 0 else s * 0

    def run(facs):
        cash = float(cap0); pos = {}; eqc = []; rebset = set(range(WARM, n - 1, STEP))
        for i in range(WARM, n):
            row = clf.iloc[i]
            eqc.append((cal[i], cash + sum(sh * (row.get(tk) or 0) for tk, sh in pos.items())))
            if i not in rebset or i + 1 >= n:
                continue
            uni = avgtv.iloc[i].dropna().sort_values(ascending=False).head(500).index
            comp = pd.Series(0.0, index=uni)
            for f in facs:
                comp = comp + zc(F[f].iloc[i].reindex(uni)).fillna(0)
            target = list(comp.sort_values(ascending=False).head(N).index)
            nrow = op.iloc[i + 1]
            eq = cash + sum(sh * (row.get(tk) or 0) for tk, sh in pos.items())
            w = eq * 0.98 / N
            for tk in list(pos):                    # 타깃 이탈분 청산
                if tk not in target:
                    p = nrow.get(tk) or row.get(tk)
                    if p and p > 0:
                        cash += pos[tk] * p * (1 - cost / 2)
                    del pos[tk]
            for tk in target:                       # 타깃 등가중 조정
                p = nrow.get(tk)
                if not (p and p > 0):
                    continue
                desired = math.floor(w / p); cur = pos.get(tk, 0); delta = desired - cur
                if delta > 0:
                    need = delta * p * (1 + cost / 2)
                    if need > cash:
                        delta = math.floor(cash / (p * (1 + cost / 2)));
                    if delta <= 0:
                        continue
                    cash -= delta * p * (1 + cost / 2); pos[tk] = cur + delta
                elif delta < 0:
                    cash += (-delta) * p * (1 - cost / 2)
                    if desired > 0:
                        pos[tk] = desired
                    else:
                        pos.pop(tk, None)
        fe = cash + sum(sh * (clf.iloc[-1].get(tk) or 0) for tk, sh in pos.items())
        eq = pd.DataFrame(eqc, columns=["d", "eq"]).set_index("d")
        yrs = (eqc[-1][0] - eqc[0][0]).days / 365.25
        cagr = (fe / cap0) ** (1 / yrs) - 1 if fe > 0 else -1
        dd = float((eq["eq"] / eq["eq"].cummax() - 1).min()); r = eq["eq"].pct_change().dropna()
        shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
        return dict(cagr=cagr, ret=fe / cap0 - 1, mdd=dd, shp=shp)

    print(f"\n=== 팩터틸트 롱온리 top{N} 월리밸 (PIT 생존편향제거 {cal[WARM].date()}~{cal[-1].date()} · 비용 {cost:.2%}) ===")
    for lbl, facs in [("저변동+대형(기존)", ["lowvol", "size"]),
                      ("+퀄리티+가치(반전X)", ["lowvol", "size", "qual", "val_ep"]),
                      ("+퀄리티+가치+반전", ["lowvol", "size", "qual", "val_ep", "rev"]),
                      ("전팩터(+B/P+52주)", ["lowvol", "size", "qual", "val_ep", "val_bp", "rev", "hi52"]),
                      ("퀄리티+가치만", ["qual", "val_ep", "val_bp"])]:
        m = run(facs)
        print(f"  {lbl:26s}: CAGR {m['cagr']:+.1%} | MDD {m['mdd']:+.1%} | Sharpe {m['shp']:.2f} | 총 {m['ret']:+.0%}")
    print("  ── 참고 ── KOSPI 보유: CAGR +16.1% MDD −39% Sharpe 0.85 · KOSPI추세추종 +12.3%/−29%/0.80 · 우리 모멘텀 +2.8%/−26%/0.32")


if __name__ == "__main__":
    main()
