"""KRX 데이터 로더 — 검증된 backtest 수집 함수를 재사용해 parquet 캐시를 구성/로드.

daily_ohlcv.parquet : 유니버스 전 종목 일봉 (date,ticker,name,market,OHLCV,trdval)
index_ohlcv.parquet : KOSPI/KOSDAQ 지수 (date,market,close) — 현재 주봉 해상도(검증 일관성)
positions.parquet   : 페이퍼 포지션 원장 (없으면 빈 원장)

거래대금(trdval)은 pykrx 일봉에 없어 종가×거래량으로 근사. 지수는 공식 API(주봉) 사용.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

from app.data.fetchers import (  # noqa: E402
    fetch_daily_ohlcv, fetch_top_liquid_universe, fetch_index_panel, load_krx_auth_key,
)

from app.data.fetchers import CACHE  # noqa: E402,F811

POSITIONS_SCHEMA = ["ticker", "name", "market", "entry_date", "entry_price", "entry_weight",
                    "shares", "holding_days", "planned_exit_date", "status",
                    "exit_date", "exit_price", "exit_reason", "realized_return", "notes"]


def build_daily_ohlcv(asof: str, fromdate: str, top_n: int, out_path: str, delay: float = 0.4) -> pd.DataFrame:
    """유니버스 전 종목 일봉을 모아 long DataFrame + parquet 저장."""
    auth = load_krx_auth_key()
    uni = fetch_top_liquid_universe(auth, asof, ["KOSPI", "KOSDAQ"], top_n, delay)
    frames = []
    for code, name, market in uni:
        d = fetch_daily_ohlcv(code, fromdate, asof, CACHE, delay)
        if d is None or len(d) < 30:
            continue
        df = d.reset_index()
        df.columns = ["date"] + list(df.columns[1:])
        df["ticker"], df["name"], df["market"] = code, name, market
        if "value" in df.columns:
            df["trdval"] = df["value"]
        else:
            df["trdval"] = df["close"] * df["volume"]   # 거래대금 근사
        frames.append(df[["date", "ticker", "name", "market", "open", "high", "low",
                          "close", "volume", "trdval"]])
    if not frames:
        raise RuntimeError("daily_ohlcv 구성 실패 (유니버스/캐시 확인)")
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    out.to_parquet(out_path, index=False)
    return out


def build_index_ohlcv(asof: str, fromdate: str, out_path: str, delay: float = 0.15) -> pd.DataFrame:
    """공식 API에서 KOSPI/KOSDAQ 지수(주봉) → index_ohlcv parquet."""
    auth = load_krx_auth_key()
    panel = fetch_index_panel(auth, fromdate, asof, CACHE, delay)
    main = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}
    rows = []
    for mk, nm in main.items():
        s = panel[(panel.market == mk) & (panel.idx_nm == nm)][["date", "close"]].copy()
        s["market"] = mk
        rows.append(s)
    out = pd.concat(rows, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    out.to_parquet(out_path, index=False)
    return out


def load_daily_ohlcv(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_index_ohlcv(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_positions(path: str) -> pd.DataFrame:
    if path and os.path.exists(path):
        df = pd.read_parquet(path)
        for c in ("entry_date", "planned_exit_date", "exit_date"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        return df
    return pd.DataFrame(columns=POSITIONS_SCHEMA)
