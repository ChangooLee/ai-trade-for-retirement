"""단타 방법론 최종 검증 v2 — 딥리서치 종합 스펙(8전략)의 문헌 정밀 규칙 구현.

전략(리서치 워크플로 종합; 파라미터 고정·무최적화):
 S1 비용인지 단기반전: skip-day 5일수익(t-6→t-1) 시장상대 하위10% & 거래량성장 VG<1
    & 하한가잠김 제외 & MAX21<10% → h=5 (de Groot 2012, Blitz 2013, Cooper 1999)
 S2 거래량동반 연속: skip-day 수익 상위20% & VG 상위20% & 당일<+10% & 상한가 제외 → h=5
    (Medhat-Schmeling 2022, Cooper 1999)
 S3 GKM 고거래 프리미엄: 당일 거래대금이 자기 50일 상위10% & |당일수익|≤5% & 20일 쿨다운 → h=5
 S4 52주 신고가 첫 돌파 + 거래량 2배 (HLY 2009) → h=5
 S5 상한가 잠김 오버나이트 (Choi-Han 2010; 종가진입 필요·체결률 미모델링 ⚠상한 해석)
 S6 급등 비상한가 [+10%,+28.5%) 페이드 확인 (Choi-Han 대조군; 회피 룰)
 S7 MAX 복권주 회피: MAX21 상위10% 매수 가정 → 음(-) 확인 (Bali-Cakici-Whitelaw 2011)
 S8 오버나이트 지속 CON21 상위10% (Lou-Polk-Skouras 2019; 종가진입⚠ 패턴 확인용)

통계: 날짜군집 보정(일별 동일가중) · 기준선과 짝지은 차이 · h>1 비중첩 · 비용 0.35%.
사용: python -m backtest.shortterm_v2_backtest [--panel top|broad]
"""
from __future__ import annotations
import argparse, math
import numpy as np, pandas as pd

HS = [1, 2, 3, 5]
COST = 0.0035


def load_panel(which):
    import yaml
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    path = cfg["paths"]["daily_ohlcv"] if which == "top" else "data/cache/broad_ohlcv.parquet"
    df = pd.read_parquet(path); df["date"] = pd.to_datetime(df["date"])
    P = {}
    for c in ("open", "high", "low", "close", "volume", "trdval"):
        m = df.pivot_table(index="date", columns="ticker", values=c)
        if c in ("open", "high", "low", "close"):
            m = m.where(m > 0)
        P[c] = m
    return P


def build(P):
    o, h, l, c, v, tv = (P[k] for k in ("open", "high", "low", "close", "volume", "trdval"))
    ret1 = c.pct_change()
    liquid = (tv.rolling(20).mean() > 1e9) & (c >= 1000)
    lock_up = (ret1 >= 0.285) & (c >= h * 0.999)          # 상한가 잠김(틱 허용)
    lock_dn = (ret1 <= -0.285) & (c <= l * 1.001)         # 하한가 잠김
    R_skip = c.shift(1) / c.shift(6) - 1                  # skip-day 5일 형성수익
    r_rel = R_skip.sub(R_skip.median(axis=1), axis=0)     # 시장상대(잔차 근사)
    VG = tv.rolling(5).mean() / tv.shift(5).rolling(20).mean()
    MAX21 = ret1.rolling(21).max()
    vol20 = v.rolling(20).mean()
    S = {}
    # S1 비용인지 반전
    S["S1 반전(상대낙폭·VG<1)"] = ((r_rel.rank(axis=1, pct=True) < 0.10) & (VG < 1.0)
                               & ~lock_dn & (MAX21 < 0.10) & liquid)
    # S2 거래량동반 연속
    S["S2 연속(수익·VG 상위20%)"] = ((R_skip.rank(axis=1, pct=True) > 0.80)
                                & (VG.rank(axis=1, pct=True) > 0.80)
                                & (ret1 < 0.10) & ~lock_up & liquid)
    # S3 GKM 고거래(완만수익)
    gkm_raw = (tv > tv.rolling(50).quantile(0.90)) & (ret1.abs() <= 0.05) & ~lock_up & ~lock_dn & liquid
    cooldown = gkm_raw.shift(1).rolling(20).sum().fillna(0) == 0
    S["S3 GKM 고거래·완만"] = gkm_raw & cooldown
    # S4 52주 신고가 첫 돌파 + 거래량
    hi252 = h.rolling(252, min_periods=200).max().shift(1)
    cross = c > hi252
    first = cross & (cross.shift(1).rolling(20).sum().fillna(0) == 0)
    S["S4 52주첫돌파·거래량2배"] = first & (v >= 2 * vol20) & ~lock_up & (ret1 < 0.10) & liquid
    # S5 상한가 잠김 (종가진입⚠)
    S["S5 상한가잠김⚠"] = lock_up & liquid
    # S6 급등 비상한가 (페이드 확인·회피룰)
    S["S6 급등비상한가(회피확인)"] = (ret1 >= 0.10) & (ret1 < 0.285) & ~lock_up & liquid
    # S7 MAX 복권주 (음(-) 확인)
    S["S7 MAX복권 상위10%(회피확인)"] = (MAX21.rank(axis=1, pct=True) > 0.90) & liquid
    # S8 오버나이트 지속 CON21 (종가진입⚠)
    con21 = np.log(o / c.shift(1)).rolling(21).sum()
    S["S8 CON21 상위10%⚠"] = (con21.rank(axis=1, pct=True) > 0.90) & liquid
    return S


def nonoverlap_est(s, h):
    k = max(1, h)
    if k == 1:
        return s.mean(), s.std(ddof=1) / math.sqrt(len(s)) if len(s) > 2 else np.nan, s
    ests, ses, best = [], [], None
    for off in range(k):
        sub = s.iloc[off::k]
        if len(sub) > 2:
            ests.append(sub.mean()); ses.append(sub.std(ddof=1) / math.sqrt(len(sub)))
            if best is None or len(sub) > len(best): best = sub
    return (float(np.mean(ests)) if ests else np.nan,
            float(np.mean(ses)) if ses else np.nan, best if best is not None else s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", choices=["top", "broad"], default="top")
    args = ap.parse_args()
    P = load_panel(args.panel)
    S = build(P)
    o, c = P["open"], P["close"]
    fwd = {h: (o.shift(-(1 + h)) / o.shift(-1) - 1) for h in HS}
    fwd[0] = (c.shift(-1) / o.shift(-1) - 1)
    fwd["ON"] = (o.shift(-1) / c - 1)
    HZ = ["ON", 0] + HS
    base = {h: fwd[h].mean(axis=1) for h in HZ}

    def hlab(h): return "밤샘⚠" if h == "ON" else ("당일" if h == 0 else f"h={h}d")
    print(f"=== v2(문헌 정밀규칙) 패널 {args.panel}: {o.shape[1]}종목 × {o.shape[0]}일 "
          f"({o.index[0].date()}~{o.index[-1].date()}) · 비용 {COST*100:.2f}% ===")
    print(f"  {'전략':<24s}{'N/일':>6s}" + "".join(f"{hlab(h):>20s}" for h in HZ))
    for name, sig in S.items():
        sig = sig.fillna(False)
        act = sig.sum(axis=1); act = act[act > 0]
        cells = []
        for h in HZ:
            dm = fwd[h].where(sig).mean(axis=1).dropna()
            ex = (dm - base[h].reindex(dm.index)).dropna()
            if len(ex) < 30:
                cells.append(f"{'—':>20s}"); continue
            mu, se, rep = nonoverlap_est(ex, h if isinstance(h, int) and h > 0 else 1)
            t = mu / se if se and se > 0 else 0
            net = dm.mean() - COST
            cells.append(f"{mu*100:+5.2f}(t{t:+4.1f}){net*100:+5.2f}n".rjust(20))
        print(f"  {name:<24s}{act.mean() if len(act) else 0:>5.1f} " + "".join(cells))
    print("\n  표기: 짝지은초과%p(t) | 자체net% · ⚠=종가진입 필요/체결 미모델링(상한 해석)")


if __name__ == "__main__":
    main()
