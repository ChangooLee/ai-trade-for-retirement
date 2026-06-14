"""매수·매도 방법 변형 비교(PIT 생존편향 제거) — 사용자 요청 2종.

방법1) 목표가/손절가 매매: 진입은 동일(F리더∩20주선 눌림), 청산에 52주 전고점 '목표가 익절' +
       스윙로우(20일 저점) '손절가'를 추가(시간40일·20주선 이탈은 백스톱으로 유지).
방법2) TDA 발굴 + 현행 매도: 진입을 TDA(위상 안정+추세 상위)로, 청산은 현행(시간40일+20주선 이탈).

비교 격리를 위해 서킷브레이커는 끄고(breaker=none), 등가중으로 통일. 기준=현행 매수·매도(모멘텀+시간/20주선).
주의: 주간 리밸런스 연구 하버스(실엔진=일단위와 다를 수 있음). 결론은 방향성 참고용.

사용: .venv/bin/python -m backtest.entry_exit_methods [--top 400]
"""
from __future__ import annotations
import argparse, sys
import numpy as np
from backtest.risk_sizing_variants import precompute, run_variant

VARIANTS = {
    "기준(모멘텀매수+시간/20주선)": {"entry": "momentum", "trend": True, "breaker": "none", "cb": 0, "stop": None, "target": False, "sizing": "equal"},
    "방법1(목표가52高+손절스윙로우)": {"entry": "momentum", "trend": True, "breaker": "none", "cb": 0, "stop": ("swing",), "target": True, "sizing": "equal"},
    "방법2(TDA발굴+현행매도)":      {"entry": "tda", "trend": True, "breaker": "none", "cb": 0, "stop": None, "target": False, "sizing": "equal"},
    "방법2+목표손절(TDA+목표/손절)":  {"entry": "tda", "trend": True, "breaker": "none", "cb": 0, "stop": ("swing",), "target": True, "sizing": "equal"},
}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--top", type=int, default=400)
    a = ap.parse_args()
    sig, meta = precompute(a.top, 5, "20170501", with_tda=True)
    rows = []
    for nm, cfg in VARIANTS.items():
        import time; t0 = time.time()
        rows.append(run_variant(nm, cfg, sig, meta))
        print(f"  ✓ {nm} {time.time()-t0:.1f}s", file=sys.stderr)
    print("\n=== 매수·매도 방법 비교 (PIT 생존편향 제거, top%d, 2017~2026, 브레이커 끔·등가중) ===" % a.top)
    print(f"{'방법':<30}{'총수익':>9}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}{'승률':>7}{'거래':>6}")
    for r in rows:
        print(f"{r['name']:<30}{r['total']:>+8.1%}{r['cagr']:>+8.1%}{r['mdd']:>+8.1%}{r['sharpe']:>8.2f}{r['win']:>7.1%}{r['ntr']:>6}")
    print("\n연도별 수익률:")
    yrs = sorted({y for r in rows for y in r["yearly"]})
    print(f"{'방법':<30}" + "".join(f"{y:>8}" for y in yrs))
    for r in rows:
        print(f"{r['name']:<30}" + "".join((f"{r['yearly'].get(y, float('nan')):>+8.1%}" if y in r['yearly'] else f"{'—':>8}") for y in yrs))


if __name__ == "__main__":
    main()
