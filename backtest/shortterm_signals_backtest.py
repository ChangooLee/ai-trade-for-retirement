"""단타(1~5거래일) 시그널 백테스트 엔진 — 일봉 OHLCV 전용, 벡터화.

설계 원칙(이전 적대 검증에서 확정된 보정 내장):
  · 신호 = day i 종가까지 데이터로 계산한 불리언 패널 → 진입 = day i+1 시가(룩어헤드 차단)
  · 수익 = open(i+1) → open(i+1+h), h∈{1,2,3,5} + 당일(시가→종가) r0
  · 통계 = 날짜별 동일가중 평균을 1관측으로(군집 제거) → 기준선과 '짝지은 차이'(시장요인 상쇄)
          h>1은 h-간격 비중첩 부분표본(시작 오프셋 평균), t값+양일비율 병기
  · 비용 = 왕복 0.35%(기본)/0.20%(낙관) — 단타 성패의 핵심
  · 패널 = 10년×300종목(생존편향: 짝지은 차이로 해석) + 최근300일×~2600종목(편향 작음) 이중 검증

사용: python -m backtest.shortterm_signals_backtest [--panel top|broad]
"""
from __future__ import annotations
import argparse, math, sys
import numpy as np, pandas as pd

HS = [1, 2, 3, 5]
COST = 0.0035


# ---------- 패널 구축 ----------
def load_panel(which):
    import yaml
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    path = cfg["paths"]["daily_ohlcv"] if which == "top" else "data/cache/broad_ohlcv.parquet"
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    P = {}
    for c in ("open", "high", "low", "close", "volume", "trdval"):
        m = df.pivot_table(index="date", columns="ticker", values=c)
        if c in ("open", "high", "low", "close"):
            m = m.where(m > 0)   # 거래정지일 가격 0 → NaN (inf 수익률 방지)
        P[c] = m
    return P


def build_features(P):
    o, h, l, c, v = P["open"], P["high"], P["low"], P["close"], P["volume"]
    F = {}
    F["ret1"] = c.pct_change()
    F["ret3"] = c.pct_change(3)
    F["ret5"] = c.pct_change(5)
    F["ma5"] = c.rolling(5).mean()
    F["ma20"] = c.rolling(20).mean()
    F["ma200"] = c.rolling(200, min_periods=120).mean()
    F["vol20"] = v.rolling(20).mean()
    F["range"] = (h - l)
    F["range_med7"] = F["range"].rolling(7).median()
    # RSI(2) — Wilder
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    F["rsi2"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    F["hi52"] = c.rolling(252, min_periods=120).max()
    F["std20"] = F["ret1"].rolling(20).std()
    return F


# ---------- 시그널 정의 (day i 종가 기준 불리언; 연구 결과로 파라미터 고정) ----------
def build_signals(P, F):
    o, h, l, c, v = P["open"], P["high"], P["low"], P["close"], P["volume"]
    up_day = F["ret1"] > 0
    liquid = P["trdval"].rolling(20).mean() > 1e9   # 20일평균 거래대금 10억+ (체결현실성)
    S = {}
    # 1) 단기 과매도 반등 (Connors RSI2): 장기추세 위 + RSI2<10
    S["RSI2<10·추세위"] = (c > F["ma200"]) & (F["rsi2"] < 10) & liquid
    # 2) 단기 손실주 반등 (3일 수익률 하위 10%, 추세 위)
    r3rank = F["ret3"].rank(axis=1, pct=True)
    S["3일낙폭 하위10%·추세위"] = (r3rank < 0.10) & (c > F["ma200"]) & liquid
    # 3) 거래량 폭발 + 양봉 (모멘텀 점화)
    S["거래량3배·종가+3%"] = (v > 3 * F["vol20"]) & (F["ret1"] > 0.03) & liquid
    # 4) 상한가/준상한가 (KR 한정 효과)
    S["급등 +25%(준상한가)"] = (F["ret1"] > 0.25) & liquid
    # 5) NR7 범위압축 + 추세 위 (변동성 수축 후 확장)
    S["NR7압축·추세위"] = (F["range"] == F["range"].rolling(7).min()) & (c > F["ma20"]) & liquid
    # 6) 5일 승자 연속 (단기 모멘텀 — 부호 검증용)
    r5rank = F["ret5"].rank(axis=1, pct=True)
    S["5일수익 상위10%"] = (r5rank > 0.90) & liquid
    # 7) 52주 신고가 돌파 직후
    S["52주 신고가 경신"] = (c >= F["hi52"]) & (c.shift(1) < F["hi52"].shift(1)) & liquid
    # 8) 첫 눌림 (강추세: 종가>MA5>MA20, 당일 음봉으로 MA5 터치)
    S["강추세 첫눌림(MA5터치)"] = ((c.shift(1) > F["ma5"].shift(1)) & (F["ma5"] > F["ma20"]) &
                              (l <= F["ma5"]) & (c > F["ma20"]) & (F["ret1"] < 0) & liquid)
    # 9) 낙폭 + 거래량 동반 (Avramov: 고거래 반전 강화)
    S["3일낙폭10%·거래량2배"] = ((F["ret3"].rank(axis=1, pct=True) < 0.10) & (v > 2 * F["vol20"]) &
                            (c > F["ma200"]) & liquid)
    # 10) 갭하락 -3% 매수(fade) — ⚠ 익일 시가 정보로 시가 직후 진입 가정
    gap_next = P["open"].shift(-1) / c - 1
    S["갭하락-3% 매수⚠"] = (gap_next < -0.03) & (c > F["ma20"]) & liquid
    # 11) 갭상승 +3% 추종 — 동일 가정
    S["갭상승+3% 추종⚠"] = (gap_next > 0.03) & up_day & liquid
    return S


# ---------- 짝지은 통계 ----------
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
    F = build_features(P)
    S = build_signals(P, F)
    o = P["open"]
    # 전방 수익: 신호일 i → 진입 open(i+1) → 청산 open(i+1+h) / r0 = 당일 시가→종가
    fwd = {h: (o.shift(-(1 + h)) / o.shift(-1) - 1) for h in HS}
    fwd[0] = (P["close"].shift(-1) / o.shift(-1) - 1)    # 진입일 시가→종가(데이트레이드)
    fwd["ON"] = (o.shift(-1) / P["close"] - 1)           # 오버나이트(신호일 종가 진입 필요⚠)
    HZ = ["ON", 0] + HS
    base_dm = {h: fwd[h].mean(axis=1) for h in HZ}

    def hlab(h):
        return "밤샘⚠" if h == "ON" else ("당일" if h == 0 else f"h={h}d")
    print(f"=== 패널 {args.panel}: {o.shape[1]}종목 × {o.shape[0]}일 "
          f"({o.index[0].date()}~{o.index[-1].date()}) · 비용 왕복 {COST*100:.2f}% ===")
    print(f"  {'시그널':<22s}{'평균N/일':>8s}" + "".join(f"{hlab(h):>21s}" for h in HZ))
    rows_csv = []
    for name, sig in S.items():
        sig = sig.fillna(False)
        npd = sig.sum(axis=1)
        active = npd[npd > 0]
        cells = []
        for h in HZ:
            r = fwd[h].where(sig)
            dm = r.mean(axis=1).dropna()                      # 날짜별 신호군 평균
            ex = (dm - base_dm[h].reindex(dm.index)).dropna() # 짝지은 차이
            if len(ex) < 30:
                cells.append(f"{'—':>21s}"); continue
            mu, se, rep = nonoverlap_est(ex, h if isinstance(h, int) and h > 0 else 1)
            t = mu / se if se and se > 0 else 0
            pos = (rep > 0).mean()
            net = dm.mean() - COST                            # 신호군 자체 net
            cells.append(f"{mu*100:+5.2f}(t{t:+4.1f}){net*100:+5.2f}n".rjust(21))
            rows_csv.append({"signal": name, "h": h, "excess": mu, "t": t,
                             "pos_share": pos, "net": net, "n_dates": len(dm),
                             "avg_picks": float(active.mean()) if len(active) else 0})
        print(f"  {name:<22s}{active.mean() if len(active) else 0:>7.1f} " + "".join(cells))
    pd.DataFrame(rows_csv).to_csv(f"backtest/shortterm_signals_{args.panel}.csv", index=False)
    print(f"\n  표기: 짝지은초과%p(t값) | 신호군 자체 net% (비용차감) · CSV 저장됨")
    print("  ※ 생존편향 패널 — '짝지은 초과'와 t값으로 판단. |t|≥2 & 일관된 부호만 신뢰.")


if __name__ == "__main__":
    main()
