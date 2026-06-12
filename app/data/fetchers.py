"""KRX 데이터 수집기 — 공식 OpenAPI(유니버스·지수) + pykrx(수정주가 일봉).

mcp-krx의 backtest/weekly_pullback_backtest.py·leader_screen.py에서 내재화.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

import pandas as pd

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis"
CACHE = os.path.expanduser("~/.ai-trade/krx_cache")

_COL_MAP = {
    "시가": "open", "고가": "high", "저가": "low", "종가": "close",
    "거래량": "volume", "거래대금": "value", "등락률": "change",
}


def _cache_path(cache_dir: str, ticker: str, fromdate: str, todate: str) -> str:
    return os.path.join(cache_dir, f"{ticker}_{fromdate}_{todate}.pkl")


def fetch_daily_ohlcv(
    ticker: str,
    fromdate: str,
    todate: str,
    cache_dir: str,
    delay: float,
    max_retries: int = 3,
) -> Optional[pd.DataFrame]:
    """종목 일봉 OHLCV 조회 (수정주가). 캐시 우선, 실패 시 재시도."""
    path = _cache_path(cache_dir, ticker, fromdate, todate)
    if os.path.exists(path):
        try:
            return pd.read_pickle(path)
        except Exception:
            pass  # 캐시 손상 시 재조회

    from pykrx import stock

    last_err = None
    for attempt in range(max_retries):
        try:
            time.sleep(delay)  # 레이트리밋
            df = stock.get_market_ohlcv_by_date(fromdate, todate, ticker, adjusted=True)
            if df is None or df.empty:
                return None
            df = df.rename(columns=_COL_MAP)
            df.index = pd.to_datetime(df.index)
            df = df[[c for c in ["open", "high", "low", "close", "volume", "value"] if c in df.columns]]
            os.makedirs(cache_dir, exist_ok=True)
            df.to_pickle(path)
            return df
        except Exception as e:  # 네트워크/파싱 오류는 종목 단위로 흡수
            last_err = e
            time.sleep(delay * (2 ** attempt))
    print(f"  ! {ticker} 조회 실패: {last_err}", file=sys.stderr)
    return None


def _load_env_key(var: str) -> str:
    """환경변수 우선, 없으면 repo 루트 .env(또는 상위 디렉터리)에서 var 로드."""
    key = os.getenv(var, "").strip()
    if key:
        return key
    here = os.path.dirname(os.path.abspath(__file__))
    # app/data → app → repo 루트 순으로 .env 탐색 (이전 구현은 app/.env만 봐서 로컬에서 못 찾던 버그)
    for up in ("..", os.path.join("..", ".."), os.path.join("..", "..", "..")):
        env_path = os.path.join(here, up, ".env")
        try:
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith(f"{var}="):
                        return s.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def load_krx_auth_key() -> str:
    """환경변수 또는 repo 루트 .env에서 KRX_AUTH_KEY 로드."""
    return _load_env_key("KRX_AUTH_KEY")


def fetch_top_liquid_universe(auth_key, bas_dd, markets, top_n, delay=0.3,
                              rank_by="trdval", min_trdval=0.0):
    """asof 단일일 단면으로 유니버스 top_n 선정 (전체 재수집 경로용).

    rank_by="trdval": 당일 거래대금 상위 top_n.
    rank_by="mktcap": 당일 거래대금 ≥ min_trdval 종목 중 시가총액 상위 top_n (결정 0003/0005).
    단일일 거래대금이라 하한은 근사 — 안정적 일일 운영은 update_data.fetch_universe_trdval20(20일 평균) 사용.
    """
    import requests
    ep = {"KOSPI": "sto/stk_bydd_trd", "KOSDAQ": "sto/ksq_bydd_trd"}
    rows = []
    for mk in markets:
        try:
            time.sleep(delay)
            r = requests.get(f"{KRX_BASE}/{ep[mk]}",
                             params={"AUTH_KEY": auth_key, "basDd": bas_dd},
                             headers={"AUTH_KEY": auth_key, "Accept": "application/json"}, timeout=30)
            if r.status_code != 200:
                print(f"  ! {ep[mk]} HTTP {r.status_code}", file=sys.stderr); continue
            for d in r.json().get("OutBlock_1", []):
                code = str(d.get("ISU_CD", "")).strip()
                if not (code.isdigit() and len(code) == 6):
                    continue
                try:
                    val = float(str(d.get("ACC_TRDVAL", "0")).replace(",", "") or 0)
                except ValueError:
                    val = 0.0
                try:
                    cap = float(str(d.get("MKTCAP", "0")).replace(",", "") or 0)
                except ValueError:
                    cap = 0.0
                rows.append((code, d.get("ISU_NM", ""), mk, val, cap))
        except Exception as e:
            print(f"  ! {ep[mk]} 실패: {e}", file=sys.stderr)
    if not rows:
        return []
    df = pd.DataFrame(rows, columns=["code", "name", "market", "trdval", "mktcap"])
    df = df[~df["name"].str.contains("우$|우B|스팩|[0-9]호$", regex=True, na=False)]
    if rank_by == "mktcap":
        df = df[(df["trdval"] >= min_trdval) & (df["mktcap"] > 0)].sort_values("mktcap", ascending=False)
    else:
        df = df.sort_values("trdval", ascending=False)
    return list(df.head(top_n)[["code", "name", "market"]].itertuples(index=False, name=None))


def fetch_index_panel(auth_key, fromdate, todate, cache_dir, delay=0.15):
    """주 단위로 kospi_dd_trd/kosdaq_dd_trd 호출 → {date,idx_nm,market,close} long DF. 캐시."""
    import requests
    path = os.path.join(cache_dir, f"index_panel_{fromdate}_{todate}.pkl")
    if os.path.exists(path):
        try:
            return pd.read_pickle(path)
        except Exception:
            pass
    fridays = pd.date_range(fromdate, todate, freq="W-FRI")
    eps = {"KOSPI": "idx/kospi_dd_trd", "KOSDAQ": "idx/kosdaq_dd_trd"}
    recs = []
    print(f"  공식 지수 수집: {len(fridays)}주 × 2시장 ...", file=sys.stderr)
    for mk, ep in eps.items():
        for i, dt in enumerate(fridays):
            bd = dt.strftime("%Y%m%d")
            try:
                time.sleep(delay)
                r = requests.get(f"{KRX_BASE}/{ep}",
                                 params={"AUTH_KEY": auth_key, "basDd": bd},
                                 headers={"AUTH_KEY": auth_key, "Accept": "application/json"}, timeout=30)
                if r.status_code != 200:
                    continue
                for x in r.json().get("OutBlock_1", []):
                    try:
                        close = float(str(x.get("CLSPRC_IDX", "")).replace(",", ""))
                    except ValueError:
                        continue
                    recs.append((pd.Timestamp(dt.date()), x.get("IDX_NM", ""), mk, close))
            except Exception:
                continue
            if (i + 1) % 100 == 0:
                print(f"    {mk} {i+1}/{len(fridays)}", file=sys.stderr)
    panel = pd.DataFrame(recs, columns=["date", "idx_nm", "market", "close"])
    os.makedirs(cache_dir, exist_ok=True)
    if not panel.empty:
        panel.to_pickle(path)
    return panel

