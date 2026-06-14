"""(a) qlib ML 신호(Alpha158+LGBM) + 우리 리스크 관리 결합 검증.

qlib 예측점수(/tmp/qlib_pred_lgbm.parquet)를 '진입 신호'로 쓰되 청산·리스크는 우리 것(시간40/20주선+월-3% 브레이커)을 적용.
질문: ML의 IC를 살리면서 qlib naive top-k의 깊은 낙폭(−47%)을 우리 리스크 관리로 줄일 수 있나?
구간: ML 예측이 있는 OOS 2022~2026. 같은 구간서 우리 룰과 비교.
실행: .venv/bin/python -m backtest.ml_plus_risk
"""
from __future__ import annotations
import sys
import pandas as pd
from backtest.risk_sizing_variants import precompute, run_variant

PRED = "/tmp/qlib_pred_lgbm.parquet"


def main():
    sig, meta = precompute(400, 5, "20170501", ml_pred_path=PRED)
    cal = meta["cal"]
    i0 = next(k for k, d in enumerate(cal) if d >= pd.Timestamp("2022-01-01"))
    i1 = max(k for k, d in enumerate(cal) if d <= pd.Timestamp("2026-06-05"))
    V = {
        "우리 룰(모멘텀+시간/20주선+월-3%)":   {"entry": "momentum", "trend": True, "breaker": "block", "cb": 0.03, "stop": None, "sizing": "equal"},
        "ML신호+우리리스크(20주선+월-3%)":     {"entry": "ml", "trend": True, "breaker": "block", "cb": 0.03, "stop": None, "sizing": "equal"},
        "ML신호+우리리스크(브레이커끔)":        {"entry": "ml", "trend": True, "breaker": "none", "cb": 0, "stop": None, "sizing": "equal"},
        "ML신호+시간청산만(20주선·브레이커끔)":  {"entry": "ml", "trend": False, "breaker": "none", "cb": 0, "stop": None, "sizing": "equal"},
        "ML신호+우리리스크+스윙손절":           {"entry": "ml", "trend": True, "breaker": "block", "cb": 0.03, "stop": ("swing",), "sizing": "equal"},
    }
    print("\n=== (a) ML 신호 + 우리 리스크 관리 (OOS 2022~2026, PIT top400, 주간, 비용0.35%) ===")
    print(f"{'전략':<34}{'총수익':>9}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}{'승률':>7}{'거래':>6}")
    for nm, cfg in V.items():
        r = run_variant(nm, cfg, sig, meta, i0=i0, i1=i1)
        print(f"{nm:<34}{r['total']:>+8.1%}{r['cagr']:>+8.1%}{r['mdd']:>+8.1%}{r['sharpe']:>8.2f}{r['win']:>7.1%}{r['ntr']:>6}", flush=True)
    print("\n참고) qlib naive ML top30(같은 OOS, 리스크관리 없음): +206%/MDD−47%/Sharpe0.75.", file=sys.stderr)


if __name__ == "__main__":
    main()
