"""시점별(point-in-time) 전종목 일별 패널 — 생존편향 없는 백테스트용.

KRX 공식 일별시세는 '그날 거래된 모든 종목'을 반환하므로(상폐 예정 종목 포함),
과거 전 거래일을 백필하면 생존편향 없는 패널이 된다. 분할/감자 등은 원시가에 점프를
만들지만 KRX가 기준가를 조정하므로 (종가-대비)=조정기준가 로 보정 팩터를 재구성한다.

산출: data/cache/pit_ohlcv.parquet (date,ticker,name,market,OHLC,volume,trdval,chg)
재시작 안전: 이미 수집된 날짜는 건너뜀, 200일마다 중간 저장.
사용: python -m app.batch.build_pit [--from 20160101] [--asof YYYYMMDD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import time

import pandas as pd

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _REPO)
from app.batch.update_data import _krx, resolve_latest_official  # noqa: E402
from app.data.fetchers import load_krx_auth_key  # noqa: E402

COLS = ["date", "ticker", "name", "market", "open", "high", "low", "close", "volume", "trdval", "chg", "mktcap"]
PIT_PATH = os.path.join(_REPO, "data/cache/pit_ohlcv_v2.parquet")   # v2: +mktcap
_EXCL = re.compile(r"우$|우B|스팩|[0-9]호$")


def _f(d, k):
    try:
        return float(str(d.get(k, "0")).replace(",", "") or 0)
    except ValueError:
        return 0.0


def _day(c, bd):
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
                         _f(d, "TDD_LWPRC"), cl, _f(d, "ACC_TRDVOL"), _f(d, "ACC_TRDVAL"),
                         _f(d, "CMPPREVDD_PRC"), _f(d, "MKTCAP")))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="20160101")
    ap.add_argument("--asof", default=None)
    args = ap.parse_args()
    auth = load_krx_auth_key()
    asof = args.asof or resolve_latest_official(auth, dt.date.today().strftime("%Y%m%d"))
    c = _krx.KRXOpenAPIClient(auth_key=auth, timeout=30)
    existing = pd.read_parquet(PIT_PATH) if os.path.exists(PIT_PATH) else pd.DataFrame(columns=COLS)
    if len(existing):
        existing["date"] = pd.to_datetime(existing["date"])
    have = set(existing["date"].dt.strftime("%Y%m%d")) if len(existing) else set()
    days = pd.date_range(args.frm, asof, freq="D")
    todo = [d.strftime("%Y%m%d") for d in days if d.weekday() < 5 and d.strftime("%Y%m%d") not in have]
    print(f"PIT 백필: {len(todo)}일 (보유 {len(have)}일) {args.frm}~{asof}", file=sys.stderr)
    frames = [existing] if len(existing) else []
    t0 = time.time(); got = 0
    for i, bd in enumerate(todo):
        rows = _day(c, bd)
        if rows:
            frames.append(pd.DataFrame(rows, columns=COLS)); got += 1
        if (i + 1) % 200 == 0:
            out = pd.concat(frames, ignore_index=True)
            out.to_parquet(PIT_PATH, index=False)
            frames = [out]
            el = time.time() - t0
            print(f"  {i+1}/{len(todo)} ({bd}) 수집 {got}거래일 · {el:.0f}s · "
                  f"잔여 ~{el/(i+1)*(len(todo)-i-1)/60:.0f}분", file=sys.stderr)
    out = pd.concat(frames, ignore_index=True).drop_duplicates(["ticker", "date"], keep="last")
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    out.to_parquet(PIT_PATH, index=False)
    print(f"완료: {out['ticker'].nunique()}종목 · {out['date'].nunique()}거래일 · {len(out)}행 → {PIT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
