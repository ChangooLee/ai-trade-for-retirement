"""KRX PIT 패널 → qlib CSV 변환(종목별 파일). 이후 dump_bin이 qlib 바이너리로 덤프.

우리 load_adjusted()는 이미 수정주가(분할/감자 역보정)라 factor=1.0으로 저장(qlib이 그대로 조정가로 취급).
출력: /tmp/qlib_krx_csv/<ticker>.csv  (date,open,high,low,close,volume,factor,money)
실행: .venv/bin/python -m backtest.qlib_tools.convert_krx
"""
from __future__ import annotations
import os, sys
import pandas as pd
sys.path.insert(0, ".")
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402

OUT = "/tmp/qlib_krx_csv"


def main():
    os.makedirs(OUT, exist_ok=True)
    daily = load_adjusted()
    daily["date"] = pd.to_datetime(daily["date"]).dt.strftime("%Y-%m-%d")
    cols = ["open", "high", "low", "close", "volume"]
    n = 0
    for tk, g in daily.groupby("ticker"):
        g = g.sort_values("date")
        if len(g) < 120:                       # 너무 짧은 종목 제외(피처 윈도 확보)
            continue
        out = g[["date"] + cols].copy()
        out["factor"] = 1.0                    # 이미 수정주가
        out["money"] = (g["close"] * g["volume"]).values
        out.to_csv(os.path.join(OUT, f"{tk}.csv"), index=False)
        n += 1
    print(f"wrote {n} symbols → {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
