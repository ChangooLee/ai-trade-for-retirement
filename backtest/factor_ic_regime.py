"""팩터 IC 국면별 분해 — '어느 시그널이 언제 작동하나'(사용자 thesis). PIT 생존편향제거.
팩터(가격·거래량·시총): 모멘텀(6-1m,12-1m)·52주고점·저변동성·사이즈(소형)·단기반전·유동성.
IC = 횡단면 Spearman(팩터 순위, forward 20일수익 순위), 매 리밸런스(20거래일). ICIR = mean/std.
국면: ①KOSPI 40주선 추세(Risk-On/Off) ②스타일 리더십(KOSPI vs KOSDAQ 13주 모멘텀) ③변동성 고/저.
※ 가치(PER/PBR)·퀄리티(ROE)·수급(외인/기관)은 별도 데이터 필요(현 PIT 미보유) → 후속.
사용: python -m backtest.factor_ic_regime
"""
from __future__ import annotations
import os, sys
import numpy as np, pandas as pd
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
import yaml  # noqa: E402
from backtest.early_cut_diagnostic import load_adjusted  # noqa: E402


def main():
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    d = load_adjusted().sort_values(["ticker", "date"])
    cl = d.pivot_table(index="date", columns="ticker", values="close")
    mc = d.pivot_table(index="date", columns="ticker", values="mktcap")
    tv = d.pivot_table(index="date", columns="ticker", values="trdval")
    cal = cl.index; ret = cl.pct_change()
    F = {}
    F["모멘텀6-1m"] = cl.shift(21) / cl.shift(126) - 1
    F["모멘텀12-1m"] = cl.shift(21) / cl.shift(252) - 1
    F["52주고점근접"] = cl / cl.rolling(252, min_periods=120).max()
    F["저변동성"] = -ret.rolling(60, min_periods=40).std()
    F["소형주(size)"] = -np.log(mc.where(mc > 0))
    F["단기반전(1m)"] = -(cl / cl.shift(21) - 1)
    F["유동성"] = np.log(tv.where(tv > 0))
    # 가치/퀄리티 (fundamental_v1 패널: PER·PBR·EPS·BPS). 가치=이익수익률(E/P)·순자산수익률(B/P), 퀄리티=ROE≈EPS/BPS.
    _fp = os.path.join(_REPO, "data/cache/fundamental_v1.parquet")
    if os.path.exists(_fp):
        fun = pd.read_parquet(_fp); fun["date"] = pd.to_datetime(fun["date"])
        per = fun.pivot_table(index="date", columns="ticker", values="per").reindex(cal, method="ffill")
        pbr = fun.pivot_table(index="date", columns="ticker", values="pbr").reindex(cal, method="ffill")
        eps = fun.pivot_table(index="date", columns="ticker", values="eps").reindex(cal, method="ffill")
        bps = fun.pivot_table(index="date", columns="ticker", values="bps").reindex(cal, method="ffill")
        F["가치(E/P)"] = 1.0 / per.where(per > 0)
        F["가치(B/P)"] = 1.0 / pbr.where(pbr > 0)
        F["퀄리티(ROE)"] = (eps / bps).where(bps > 0)
    fwd = cl.shift(-20) / cl - 1
    avgtv = tv.rolling(20, min_periods=10).mean()
    # 국면 — 주봉 인덱스를 일봉 cal로 ffill
    idx = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    def iser(mk):
        g = idx[idx["market"] == mk].sort_values("date").set_index("date")["close"].astype(float)
        return g.reindex(cal, method="ffill")
    ko, kq = iser("KOSPI"), iser("KOSDAQ")
    ko_ma = ko.rolling(200, min_periods=120).mean()             # ~40주(200거래일) 추세선
    ko_trend = ko > ko_ma                                       # Risk-On
    lead_kospi = (ko / ko.shift(65) - 1) >= (kq / kq.shift(65) - 1)   # 13주(65거래일) 상대모멘텀
    kovol = ko.pct_change().rolling(65).std()
    vol_hi = kovol > kovol.median()
    rebals = [cal[i] for i in range(252, len(cal) - 21, 20)]
    recs = []
    for dt in rebals:
        uni = avgtv.loc[dt].dropna().sort_values(ascending=False).head(500).index
        fr = fwd.loc[dt, uni]
        row = {"date": dt, "trend": bool(ko_trend.loc[dt]), "lead": "KOSPI" if bool(lead_kospi.loc[dt]) else "KOSDAQ",
               "vol": "고변동" if bool(vol_hi.loc[dt]) else "저변동"}
        for nm, M in F.items():
            fv = M.loc[dt].reindex(uni); v = fv.notna() & fr.notna()
            row[nm] = (fv[v].rank().corr(fr[v].rank())) if v.sum() > 30 else np.nan
        recs.append(row)
    R = pd.DataFrame(recs)
    facs = list(F.keys())

    def block(title, sub):
        print(f"\n[{title}] (리밸런스 {len(sub)}회)")
        print(f"  {'팩터':14s} {'평균IC':>8s} {'ICIR':>7s} {'양의비율':>8s}")
        for f in facs:
            s = sub[f].dropna()
            if len(s) < 5:
                continue
            ic = s.mean(); icir = ic / s.std() if s.std() > 0 else 0; pos = (s > 0).mean()
            print(f"  {f:14s} {ic:+8.3f} {icir:+7.2f} {pos:7.0%}")

    print(f"=== 팩터 IC 국면별 (PIT 생존편향제거 · {str(cal[0].date())}~{str(cal[-1].date())} · forward 20일 · 유니버스 상위500) ===")
    print("※ |IC|>0.03 + ICIR>0.3 이면 의미 있는 예측력(통상 기준). 양의비율=IC>0인 리밸런스 비율.")
    block("전체", R)
    block("국면① KOSPI 추세 위(Risk-On)", R[R["trend"]])
    block("국면① KOSPI 추세 아래(Risk-Off)", R[~R["trend"]])
    block("국면② KOSPI 리더십(대형 주도)", R[R["lead"] == "KOSPI"])
    block("국면② KOSDAQ 리더십(중소형 주도)", R[R["lead"] == "KOSDAQ"])
    block("국면③ 고변동", R[R["vol"] == "고변동"])
    block("국면③ 저변동", R[R["vol"] == "저변동"])


if __name__ == "__main__":
    main()
