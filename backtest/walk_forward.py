"""워크포워드 검증 — 변형 비교(risk_sizing_variants)의 결론이 과최적화인지 아웃샘플에서 확인.

Part 1) 구간별 독립 비교: 2년 단위 연속 폴드마다 신규자본으로 A/C/손절/컨빅션을 돌려
        "C가 A를 이긴다 / 컨빅션은 이득 없다"가 레짐(상승·하락장)에 견고한지 본다.
Part 2) 앵커드 워크포워드(진짜 OOS): 매 연도에 대해 그 이전(IS)만으로 브레이커 설정을
        Sharpe 기준 선택 → 보지 못한 그 해(OOS)에 적용. 선택 전략 vs 현행-A 고정 vs C 고정 비교.
        → "과거로 고른 최적값이 미래에 통하는가"를 직접 측정(임계값 과최적 함정 검증).

사용: .venv/bin/python -m backtest.walk_forward [--top 400]
"""
from __future__ import annotations
import argparse, sys
import numpy as np, pandas as pd
from backtest.risk_sizing_variants import precompute, run_variant

# 후보 설정 (20주선+시간청산은 항상 ON=견고; 브레이커 모드×한도만 선택, 등가중)
CANDS = {
    "차단-3%(현행A)": {"trend": True, "breaker": "block", "cb": 0.03, "stop": None, "sizing": "equal"},
    "차단-5%":       {"trend": True, "breaker": "block", "cb": 0.05, "stop": None, "sizing": "equal"},
    "청산-3%(C)":    {"trend": True, "breaker": "liq", "cb": 0.03, "stop": None, "sizing": "equal"},
    "청산-5%":       {"trend": True, "breaker": "liq", "cb": 0.05, "stop": None, "sizing": "equal"},
    "브레이커끔":     {"trend": True, "breaker": "none", "cb": 0.0, "stop": None, "sizing": "equal"},
}
A_CFG = CANDS["차단-3%(현행A)"]; C_CFG = CANDS["청산-3%(C)"]
FOLD_VARIANTS = {
    "A 현행(차단)":      A_CFG,
    "C 전량청산":        C_CFG,
    "B 고정손절-8%":     {"trend": True, "breaker": "block", "cb": 0.03, "stop": ("pct", 0.08), "sizing": "equal"},
    "B 스윙로우손절":     {"trend": True, "breaker": "block", "cb": 0.03, "stop": ("swing",), "sizing": "equal"},
    "비중:컨빅션":       {"trend": True, "breaker": "block", "cb": 0.03, "stop": None, "sizing": "conviction"},
}


def idx_from(cal, y, first=True):
    ys = [k for k, d in enumerate(cal) if d.year == y]
    return (ys[0] if first else ys[-1]) if ys else None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--top", type=int, default=400)
    a = ap.parse_args()
    sig, meta = precompute(a.top, 5, "20170501")
    cal = meta["cal"]
    years = sorted({d.year for d in cal})

    # ---------- Part 1: 구간별 독립 폴드 ----------
    folds = [(2017, 2018), (2019, 2020), (2021, 2022), (2023, 2024), (2025, 2026)]
    print("\n=== Part 1) 구간별 독립 비교 (각 폴드 신규 1천만원, 총수익 / MDD / Sharpe) ===")
    hdr = "변형".ljust(18) + "".join(f"{f'{y0}-{str(y1)[2:]}':>22}" for y0, y1 in folds)
    print(hdr)
    fold_res = {nm: [] for nm in FOLD_VARIANTS}
    for nm, cfg in FOLD_VARIANTS.items():
        cells = []
        for y0, y1 in folds:
            i0 = idx_from(cal, y0, True); i1 = idx_from(cal, y1, False)
            r = run_variant(nm, cfg, sig, meta, i0=i0, i1=i1)
            fold_res[nm].append(r)
            cells.append(f"{r['total']:>+7.0%}/{r['mdd']:>+5.0%}/{r['sharpe']:>4.2f}")
        print(nm.ljust(18) + "".join(f"{c:>22}" for c in cells))
    # 견고성 집계: C vs A
    a_r, c_r = fold_res["A 현행(차단)"], fold_res["C 전량청산"]
    n = len(folds)
    c_ret = sum(c_r[k]["total"] > a_r[k]["total"] for k in range(n))
    c_mdd = sum(c_r[k]["mdd"] > a_r[k]["mdd"] for k in range(n))      # mdd는 음수 — 큰 값(덜 빠짐)이 좋음
    c_shp = sum(c_r[k]["sharpe"] > a_r[k]["sharpe"] for k in range(n))
    conv_r = fold_res["비중:컨빅션"]
    conv_shp = sum(conv_r[k]["sharpe"] > a_r[k]["sharpe"] for k in range(n))
    print(f"\n  C(전량청산)가 A(현행)보다 나은 폴드 수 — 수익 {c_ret}/{n} · 낙폭(덜빠짐) {c_mdd}/{n} · Sharpe {c_shp}/{n}")
    print(f"  컨빅션 사이징이 A보다 Sharpe 높은 폴드 수 — {conv_shp}/{n}")

    # ---------- Part 2: 앵커드 워크포워드(OOS 선택) ----------
    oos_years = [y for y in years if y >= 2020]
    print("\n=== Part 2) 앵커드 워크포워드 — 그 해 이전(IS)만으로 Sharpe 최고 설정 선택 → 그 해(OOS) 적용 ===")
    print(f"{'OOS연도':<8}{'IS선택설정':<16}{'선택OOS':>10}{'현행A OOS':>11}{'C고정 OOS':>11}")
    adap, fixA, fixC, picks = [], [], [], []
    for oy in oos_years:
        is_i1 = idx_from(cal, oy - 1, False)
        if is_i1 is None: continue
        # IS(데이터시작~oy-1말)에서 후보별 Sharpe → 최고 선택
        scored = []
        for cn, cfg in CANDS.items():
            r = run_variant(cn, cfg, sig, meta, i0=None, i1=is_i1)
            scored.append((r["sharpe"], cn, cfg))
        scored.sort(reverse=True); _, pick_name, pick_cfg = scored[0]
        # OOS(그 해) 적용
        o0, o1 = idx_from(cal, oy, True), idx_from(cal, oy, False)
        sel = run_variant(pick_name, pick_cfg, sig, meta, i0=o0, i1=o1)["total"]
        ra = run_variant("A", A_CFG, sig, meta, i0=o0, i1=o1)["total"]
        rc = run_variant("C", C_CFG, sig, meta, i0=o0, i1=o1)["total"]
        adap.append(sel); fixA.append(ra); fixC.append(rc); picks.append(pick_name)
        print(f"{oy:<8}{pick_name:<16}{sel:>+10.1%}{ra:>+11.1%}{rc:>+11.1%}")
    def comp(rs):
        v = 1.0
        for x in rs: v *= (1 + x)
        return v - 1
    print(f"\n  OOS {oos_years[0]}~{oos_years[-1]} 누적(복리):  선택전략 {comp(adap):+.0%}  ·  현행A 고정 {comp(fixA):+.0%}  ·  C 고정 {comp(fixC):+.0%}")
    print(f"  OOS 연도별 최악:  선택 {min(adap):+.0%}  ·  현행A {min(fixA):+.0%}  ·  C {min(fixC):+.0%}")
    from collections import Counter
    print(f"  IS가 매년 고른 설정 분포: {dict(Counter(picks))}")


if __name__ == "__main__":
    main()
