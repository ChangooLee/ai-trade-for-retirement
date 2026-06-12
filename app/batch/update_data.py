"""증분 데이터 업데이트 (일일 배치용) — 기존 parquet에 누락된 최신 거래일만 덧붙인다.

10년 전체 재다운로드 없이 빠르게 최신화:
- 유니버스: 공식 API 유동성 상위(top_n) 매일 재산정 (가벼움, 2콜)
- 일봉:    parquet에 있는 종목은 누락분만 pykrx(수정주가)로 증분 조회·append.
           신규 편입 종목만 전체 이력 조회. 유니버스 이탈 종목은 제외.
- 지수:    공식 API 주봉을 마지막 보유주 다음부터 asof까지만 증분 조회·append.

분할/배당 재수정은 증분으로 반영되지 않으므로, 주기적으로
`python -m app.batch.build_webapp --refresh`(전체 재수집)로 재기준화 권장.

사용: python -m app.batch.update_data [--asof YYYYMMDD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import sys
import time

import pandas as pd
import yaml

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _REPO)
from app.data import krx_loader as L  # noqa: E402
from app.data.krx_loader import CACHE  # noqa: E402
from app.data.fetchers import fetch_top_liquid_universe, fetch_index_panel, load_krx_auth_key  # noqa: E402
from app.data.fetchers import _COL_MAP  # noqa: E402

from app.data import krx_api as _krx  # noqa: E402

DAILY_COLS = ["date", "ticker", "name", "market", "open", "high", "low", "close", "volume", "trdval"]


def resolve_latest_official(auth, today_str, back=8):
    """시스템 날짜에서 역순으로 공식 API에 EOD가 있는 최신 거래일을 찾는다."""
    c = _krx.KRXOpenAPIClient(auth_key=auth, timeout=20)
    base = dt.datetime.strptime(today_str, "%Y%m%d")
    for i in range(back):
        bd = (base - dt.timedelta(days=i)).strftime("%Y%m%d")
        try:
            if c.call("stk_bydd_trd", bd):
                return bd
        except Exception:
            pass
    return today_str


def fetch_universe_trdval20(auth, asof_str, markets, top_n, lookback=20, max_back=40):
    """최근 lookback 거래일 '평균 거래대금' 상위 top_n 종목 (안정적 유니버스).

    1일 거래대금 순위는 매일 출렁여 데일리 화면 종목이 크게 바뀐다(config 의도는 trdval20).
    공식 API 전종목 일별시세를 최근 20거래일 모아 평균 거래대금으로 랭킹 → churn 최소화.
    """
    c = _krx.KRXOpenAPIClient(auth_key=auth, timeout=20)
    eps = {"KOSPI": "stk_bydd_trd", "KOSDAQ": "ksq_bydd_trd"}
    base = dt.datetime.strptime(asof_str, "%Y%m%d")
    acc = {}  # code -> [name, market, sum_trdval, n]
    days = i = 0
    while days < lookback and i < max_back:
        bd = (base - dt.timedelta(days=i)).strftime("%Y%m%d"); i += 1
        got = False
        for mk in markets:
            try:
                recs = c.call(eps[mk], bd)
            except Exception:
                recs = []
            if not recs:
                continue
            got = True
            for d in recs:
                code = str(d.get("ISU_CD", "")).strip()
                if not (code.isdigit() and len(code) == 6):
                    continue
                try:
                    val = float(str(d.get("ACC_TRDVAL", "0")).replace(",", "") or 0)
                except ValueError:
                    val = 0.0
                e = acc.get(code)
                if e is None:
                    acc[code] = [d.get("ISU_NM", ""), mk, val, 1]
                else:
                    e[2] += val; e[3] += 1
        if got:
            days += 1
    rows = [(code, v[0], v[1], v[2] / v[3]) for code, v in acc.items() if v[3]]
    if not rows:
        return []
    df = pd.DataFrame(rows, columns=["code", "name", "market", "avgval"])
    df = df[~df["name"].str.contains("우$|우B|스팩|[0-9]호$", regex=True, na=False)]
    return list(df.sort_values("avgval", ascending=False).head(top_n)
                [["code", "name", "market"]].itertuples(index=False, name=None))


def _fetch_slice(stock, code, start, end, delay, retries=3):
    """pykrx 수정주가 일봉 슬라이스 (start~end). 실패 시 지수 백오프 재시도."""
    for a in range(retries):
        try:
            time.sleep(delay)
            d = stock.get_market_ohlcv_by_date(start, end, code, adjusted=True)
            if d is None or d.empty:
                return None
            d = d.rename(columns=_COL_MAP)
            d.index = pd.to_datetime(d.index)
            return d[[c for c in ["open", "high", "low", "close", "volume", "value"] if c in d.columns]]
        except Exception:
            time.sleep(delay * (2 ** a))
    return None


def _lookup_names(auth, asof_str, codes):
    """관리 지정 종목 등 임의 코드의 (이름, 시장) 조회 — 공식 일별시세에서."""
    want = set(codes)
    if not want:
        return {}
    c = _krx.KRXOpenAPIClient(auth_key=auth, timeout=20)
    out = {}
    for mk, ep in (("KOSPI", "stk_bydd_trd"), ("KOSDAQ", "ksq_bydd_trd")):
        try:
            recs = c.call(ep, asof_str)
        except Exception:
            recs = []
        for d in recs:
            code = str(d.get("ISU_CD", "")).strip()
            if code in want:
                out[code] = (d.get("ISU_NM", ""), mk)
    return out


def update_daily_ohlcv(asof_str, fromdate, top_n, dpath, delay=0.4, managed=None):
    from pykrx import stock
    auth = load_krx_auth_key()
    uni = fetch_universe_trdval20(auth, asof_str, ["KOSPI", "KOSDAQ"], top_n)
    if not uni:
        raise RuntimeError("유니버스 조회 실패 (공식 API 확인)")
    # 관리 지정 종목: 거래대금 순위 밖이어도 항상 포함(풀 분석 대상)
    managed = [str(t).zfill(6) for t in (managed or [])]
    have = {c for c, _, _ in uni}
    miss = [c for c in managed if c not in have]
    if miss:
        nm = _lookup_names(auth, asof_str, miss)
        for c in miss:
            n, m = nm.get(c, (c, ""))
            uni.append((c, n, m))
        print(f"  + 관리 지정 {len(miss)}종목 포함: {miss}", file=sys.stderr)
    codes = [c for c, _, _ in uni]
    asof_dt = pd.to_datetime(asof_str)
    existing = L.load_daily_ohlcv(dpath) if os.path.exists(dpath) else pd.DataFrame(columns=DAILY_COLS)
    base = existing[existing["ticker"].isin(codes)].copy() if len(existing) else existing
    last_by = base.groupby("ticker")["date"].max().to_dict() if len(base) else {}
    frames, upd, new, rows = [], 0, 0, 0
    for code, name, market in uni:
        last = last_by.get(code)
        if last is not None and last >= asof_dt:
            continue  # 이미 최신
        start = (last + pd.Timedelta(days=1)).strftime("%Y%m%d") if last is not None else fromdate
        d = _fetch_slice(stock, code, start, asof_str, delay)
        if d is None:
            continue
        d = d.reset_index()
        d.columns = ["date"] + list(d.columns[1:])
        d["ticker"], d["name"], d["market"] = code, name, market
        d["trdval"] = d["value"] if "value" in d.columns else d["close"] * d["volume"]
        frames.append(d[DAILY_COLS])
        rows += len(d)
        new += last is None
        upd += last is not None
    out = pd.concat([base] + frames, ignore_index=True) if frames else base
    if not len(out):
        raise RuntimeError("일봉 업데이트 결과가 비어있음")
    out["date"] = pd.to_datetime(out["date"])
    out = out.drop_duplicates(["ticker", "date"], keep="last").sort_values(["ticker", "date"]).reset_index(drop=True)
    out.to_parquet(dpath, index=False)
    print(f"[일봉] 유니버스 {len(uni)} · 증분 {upd}종목 · 신규 {new}종목 · +{rows}행 · "
          f"최신 {out['date'].max().date()}", file=sys.stderr)
    return out


def update_index_ohlcv(asof_str, fromdate, ipath, delay=0.15):
    auth = load_krx_auth_key()
    existing = L.load_index_ohlcv(ipath) if os.path.exists(ipath) else pd.DataFrame(columns=["date", "market", "close"])
    last = existing["date"].max() if len(existing) else None
    start = (last + pd.Timedelta(days=1)).strftime("%Y%m%d") if last is not None else fromdate
    if pd.to_datetime(start) > pd.to_datetime(asof_str):
        print(f"[지수] 이미 최신 ({last.date()})", file=sys.stderr)
        return existing
    panel = fetch_index_panel(auth, start, asof_str, CACHE, delay)
    main = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}
    rows = []
    for mk, nm in main.items():
        s = panel[(panel.market == mk) & (panel.idx_nm == nm)][["date", "close"]].copy()
        s["market"] = mk
        rows.append(s)
    new = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["date", "market", "close"])
    if not len(new):
        last_lbl = existing["date"].max().date() if len(existing) else "—"
        print(f"[지수] 신규 주봉 없음 · 최신 {last_lbl}", file=sys.stderr)
        return existing
    out = pd.concat([existing, new], ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    out = out.drop_duplicates(["market", "date"], keep="last").sort_values(["market", "date"]).reset_index(drop=True)
    out.to_parquet(ipath, index=False)
    print(f"[지수] +{len(new)}행 · 최신 {out['date'].max().date()}", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None)
    ap.add_argument("--config", default="config/strategy.yaml")
    ap.add_argument("--from", dest="fromdate", default="20160101")
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    auth = load_krx_auth_key()
    today = dt.date.today().strftime("%Y%m%d")
    asof = args.asof or resolve_latest_official(auth, today)
    dpath, ipath = cfg["paths"]["daily_ohlcv"], cfg["paths"]["index_ohlcv"]
    print(f"증분 업데이트 시작: asof={asof} (시스템 {today})", file=sys.stderr)
    managed = cfg["universe"].get("managed_tickers", [])
    update_daily_ohlcv(asof, args.fromdate, cfg["universe"]["top_n_by_trdval20"], dpath, args.delay, managed)
    update_index_ohlcv(asof, args.fromdate, ipath, max(args.delay / 2, 0.1))
    print("증분 업데이트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
