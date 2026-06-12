"""전 상장종목 일별 OHLCV(공식 API, rolling N거래일) — 유니버스 밖 종목 '풀 분석'용.

공식 stk_bydd_trd/ksq_bydd_trd는 전 종목 일별시세를 시장당 1콜로 주므로, 전 시장 히스토리를
하루 2콜로 구축·증분 갱신한다. pykrx 수정주가가 아닌 원시가(분할/배당 미수정)라 차트는 제공하지
않고 지표·TDA 신호 계산에만 쓴다(상위 유니버스는 별도 pykrx 수정주가 parquet 유지).

산출: data/cache/broad_ohlcv.parquet (date,ticker,name,market,OHLC,volume,trdval) 최근 N거래일.
사용: python -m app.batch.build_broad [--asof YYYYMMDD] [--days 300]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

import pandas as pd
import yaml

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _REPO)
from app.batch.update_data import _krx, resolve_latest_official  # noqa: E402
from app.data.fetchers import load_krx_auth_key  # noqa: E402

COLS = ["date", "ticker", "name", "market", "open", "high", "low", "close", "volume", "trdval"]
BROAD_PATH = os.path.join(_REPO, "data/cache/broad_ohlcv.parquet")
_EXCL = re.compile(r"우$|우B|스팩|[0-9]호$")


def _f(d, k):
    try:
        return float(str(d.get(k, "0")).replace(",", "") or 0)
    except ValueError:
        return 0.0


def _day_allstocks(c, bd):
    rows = []
    for mk, ep in (("KOSPI", "stk_bydd_trd"), ("KOSDAQ", "ksq_bydd_trd")):
        try:
            recs = c.call(ep, bd)
        except Exception:
            recs = []
        for d in recs:
            code = str(d.get("ISU_CD", "")).strip()
            nm = d.get("ISU_NM", "")
            if not (code.isdigit() and len(code) == 6) or not nm or _EXCL.search(nm):
                continue
            cl = _f(d, "TDD_CLSPRC")
            if cl <= 0:
                continue
            rows.append((pd.Timestamp(bd), code, nm, mk, _f(d, "TDD_OPNPRC"), _f(d, "TDD_HGPRC"),
                         _f(d, "TDD_LWPRC"), cl, _f(d, "ACC_TRDVOL"), _f(d, "ACC_TRDVAL")))
    return rows


def build_broad(asof_str, out_path=BROAD_PATH, days=300, max_back=480):
    auth = load_krx_auth_key()
    c = _krx.KRXOpenAPIClient(auth_key=auth, timeout=20)
    existing = pd.read_parquet(out_path) if os.path.exists(out_path) else pd.DataFrame(columns=COLS)
    if len(existing):
        existing["date"] = pd.to_datetime(existing["date"])
    have = set(existing["date"].dt.strftime("%Y%m%d")) if len(existing) else set()
    base = dt.datetime.strptime(asof_str, "%Y%m%d")
    frames = [existing] if len(existing) else []
    got = i = new_days = 0
    while got < days and i < max_back:
        bd = (base - dt.timedelta(days=i)).strftime("%Y%m%d"); i += 1
        if bd in have:
            got += 1; continue           # 이미 보유한 거래일
        rows = _day_allstocks(c, bd)
        if not rows:
            continue                      # 휴장일
        frames.append(pd.DataFrame(rows, columns=COLS)); got += 1; new_days += 1
        if new_days % 50 == 0:
            print(f"  수집 {new_days}일 (…{bd})", file=sys.stderr)
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    keep = sorted(out["date"].unique())[-days:]                 # 최근 days 거래일만 유지(rolling)
    out = out[out["date"].isin(keep)].drop_duplicates(["ticker", "date"], keep="last")
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    out.to_parquet(out_path, index=False)
    print(f"[브로드] 전종목 {out['ticker'].nunique()} · {len(keep)}거래일 · 신규 {new_days}일 · {len(out)}행 → {out_path}", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None)
    ap.add_argument("--days", type=int, default=300)
    args = ap.parse_args()
    auth = load_krx_auth_key()
    asof = args.asof or resolve_latest_official(auth, dt.date.today().strftime("%Y%m%d"))
    build_broad(asof, days=args.days)


if __name__ == "__main__":
    main()
