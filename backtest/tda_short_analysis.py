"""단타 백테스트 사후 분석 — 통계 보정판 (적대 검증 워크플로 확정 결함 반영).

보정/진단:
  1) 날짜 군집: 종목 풀링 대신 '날짜별 그룹 동일가중 평균'을 1관측으로(가짜 N 제거).
  2) 짝지은 차이: D_t = 그룹평균_t − 기준선평균_t → 같은 날 시장요인 상쇄. t값+부호검정.
  3) 중첩 윈도(h>step): ceil(h/step) 간격 비중첩 부분표본(시작 오프셋 평균).
  4) 집중도: top8 풀링 평균에서 상위 1% 레코드 기여율(소수 에피소드 의존 진단).
  5) 탈락률: r1 유효인데 r_h NaN(거래정지 등) 비율 — 그룹 간 차이 크면 비교 왜곡.
  ※ sell8(회피검증)은 생존편향이 '덜 나쁘게' 보이게 만드므로 상한 해석만.
"""
from __future__ import annotations
import math, sys
import numpy as np, pandas as pd

HS = [1, 2, 3, 5, 10, 20, 40]
COST = 0.0035
STEP = 5

R = pd.read_csv(sys.argv[1] if len(sys.argv) > 1 else "backtest/tda_short_records.csv")
R["date"] = pd.to_datetime(R["date"])
groups = [("기준선", R), ("모멘텀A", R[R.A]), ("TDA적격", R[R.elig]),
          ("TDA top8", R[R.top8]), ("TDA 신규신호", R[R.fresh]), ("TDA 매도군†", R[R.sell8])]


def date_means(g, h):
    return g.groupby("date")[f"r{h}"].mean().dropna()


def nonoverlap(s, h):
    """비중첩 부분표본 — 모든 시작 오프셋의 추정치 평균(리뷰 권고)."""
    k = max(1, math.ceil(h / STEP))
    if k == 1:
        return s, s.mean(), s.std(ddof=1) / np.sqrt(len(s)) if len(s) > 2 else np.nan
    ests, ses, reps = [], [], []
    for off in range(k):
        sub = s.iloc[off::k]
        if len(sub) > 2:
            ests.append(sub.mean()); ses.append(sub.std(ddof=1) / np.sqrt(len(sub))); reps.append(sub)
    rep = reps[int(np.argmin([abs(e) for e in ses]))] if reps else s
    return rep, float(np.mean(ests)) if ests else np.nan, float(np.mean(ses)) if ses else np.nan


print("=== ① net 평균(일별 동일가중·비중첩 보정, 비용 0.35% 차감) — 그룹 자체 수익 ===")
print(f"  {'그룹':<12s}" + "".join(f"{'h='+str(h):>19s}" for h in HS))
for lab, g in groups:
    cells = []
    for h in HS:
        dm = date_means(g, h)
        if len(dm) < 15: cells.append(f"{'—':>19s}"); continue
        _, mu, se = nonoverlap(dm, h)
        t = mu / se if se and se > 0 else 0
        cells.append(f"{(mu-COST)*100:+6.2f}%(t{t:+4.1f})".rjust(19))
    print(f"  {lab:<12s}" + "".join(cells))

print("\n=== ② 기준선 대비 초과수익 D_t (짝지은 차이 — 시장요인 상쇄) · t값 · 양(+)일 비율 ===")
for lab, g in groups[1:]:
    cells = []
    for h in HS:
        a = g.groupby("date")[f"r{h}"].mean()
        b = R.groupby("date")[f"r{h}"].mean()
        ex = (a - b).dropna()
        if len(ex) < 15: cells.append(f"{'—':>19s}"); continue
        rep, mu, se = nonoverlap(ex, h)
        t = mu / se if se and se > 0 else 0
        pos = (rep > 0).mean()
        cells.append(f"{mu*100:+5.2f}(t{t:+4.1f}·{pos*100:2.0f}%)".rjust(19))
    print(f"  {lab:<12s}" + "".join(cells))
print("  ※ |t|≥2 & 양일비율 55%+ 정도여야 우연이 아니라고 볼 수 있음. 비용은 양쪽 동일이라 미차감.")

print("\n=== ③ 집중도 진단: top8 풀링 평균에서 상위 1% 레코드 기여율 ===")
g = R[R.top8]
for h in (3, 5, 10, 20):
    f = g[f"r{h}"].dropna().sort_values(ascending=False)
    if len(f) < 50: continue
    k = max(1, int(len(f) * 0.01))
    contrib = f.head(k).sum() / f.sum() if f.sum() != 0 else np.nan
    print(f"  h={h}d: 전체합 중 상위1%({k}건) 기여 {contrib*100:.0f}% · 평균 {f.mean()*100:+.2f}% → 상위1% 제외 시 {f.iloc[k:].mean()*100:+.2f}%")

print("\n=== ④ 탈락률(거래정지 등: r1 유효 & r_h NaN) — 그룹 간 격차 크면 비교 주의 ===")
for lab, g in groups:
    v = g[g["r1"].notna()]
    rates = [f"h{h}:{(v[f'r{h}'].isna().mean())*100:.1f}%" for h in (10, 20, 40)]
    print(f"  {lab:<12s} " + " · ".join(rates))

print("\n=== ⑤ TDA top8 연도별 (h=5d, net, 날짜평균 기준) ===")
g = R[R.top8].copy()
dm = g.groupby("date")["r5"].mean().dropna()
for yr, s in dm.groupby(dm.index.year):
    print(f"  {yr}: 리밸런스 {len(s):3d}회 · net평균 {(s.mean()-COST)*100:+5.2f}%/회 · 양(+)회 {(s>COST).mean()*100:3.0f}%")
print("\n† sell8(매도군)은 생존편향이 결과를 '덜 나쁘게' 보이게 함 — 회피 가치의 상한으로만 해석.")
