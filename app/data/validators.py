"""데이터 품질 검증 (§4). 매수/매도 신뢰도 배지의 근거."""
from __future__ import annotations

import pandas as pd


def validate_daily_ohlcv(df: pd.DataFrame, asof: pd.Timestamp) -> dict:
    errors, warnings = [], []
    at = df[df["date"] == asof]
    if at.empty:
        errors.append(f"기준일({asof.date()}) 데이터 없음")
    for col in ("open", "high", "low", "close"):
        if (at[col] <= 0).any():
            errors.append(f"{col} 0/음수 존재")
    bad_hi = at[at["high"] < at[["open", "close"]].max(axis=1)]
    bad_lo = at[at["low"] > at[["open", "close"]].min(axis=1)]
    if len(bad_hi):
        errors.append(f"high < max(open,close) {len(bad_hi)}건")
    if len(bad_lo):
        errors.append(f"low > min(open,close) {len(bad_lo)}건")
    if (at["trdval"].isna() | (at["trdval"] < 0)).any():
        warnings.append("거래대금 null/음수 일부")
    dup = df.duplicated(subset=["ticker", "date"]).sum()
    if dup:
        errors.append(f"ticker/date 중복 {dup}건")
    # 전일 대비 ±30% 초과 (상하한/권리락 플래그)
    prev = df.sort_values("date").groupby("ticker")["close"].shift(1)
    chg = (df["close"] / prev - 1).abs()
    extreme = ((df["date"] == asof) & (chg > 0.30)).sum()
    if extreme:
        warnings.append(f"전일대비 ±30% 초과 {extreme}건(상하한/권리락 확인)")
    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings,
            "row_count": int(len(df)), "ticker_count": int(at["ticker"].nunique())}


def validate_index_ohlcv(df: pd.DataFrame, asof: pd.Timestamp) -> dict:
    errors = []
    for mk in ("KOSPI", "KOSDAQ"):
        s = df[(df.market == mk) & (df.date <= asof)]
        if s.empty:
            errors.append(f"{mk} 지수 데이터 없음")
        elif (s["close"] <= 0).any():
            errors.append(f"{mk} 지수 종가 0/음수")
    return {"ok": len(errors) == 0, "errors": errors}


def validate_all(daily, index, asof, cache_status="fresh") -> dict:
    d = validate_daily_ohlcv(daily, asof)
    i = validate_index_ohlcv(index, asof)
    ok = d["ok"] and i["ok"]
    status = "ok" if ok else "warning"
    if cache_status == "cached":
        status = "cached_warning"
    return {"status": status, "daily": d, "index": i,
            "daily_rows": d["row_count"], "ticker_count": d["ticker_count"],
            "index_status": "ok" if i["ok"] else "error", "cache_status": cache_status,
            "errors": d["errors"] + i["errors"], "warnings": d.get("warnings", [])}
