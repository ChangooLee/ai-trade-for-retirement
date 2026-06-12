"""공시 기반 악재 필터 — 매수 후보의 최근 공시를 위험도 분류해 화면에 경고.

배경: PIT 백테스트에서 큰 꼬리손실(−30%~전손)의 상당수가 공시 동반 이벤트
(상장적격성·감사의견·유상증자 등)였음. LLM 없이 보고서명 패턴으로 1차 분류.

분류:
  crit  상장폐지·거래정지급 — 매수 금지 권고 (상장적격성, 감사의견, 관리종목,
        회생/파산, 횡령·배임, 자본잠식, 불성실공시)
  warn  희석·수급 악재 — 주의 (유상증자, CB/BW 발행·전환, 감자, 소송, 단일판매계약해지)
"""
from __future__ import annotations

import datetime as dt
import re
import sys

from app.dart.client import corp_code_map, disclosures

CRIT = re.compile(r"상장폐지|상장적격성|관리종목|감사의견|의견거절|감사보고서\s*제출\s*지연|"
                  r"회생절차|파산|해산|횡령|배임|자본잠식|불성실공시|거래정지|개선기간")
WARN = re.compile(r"유상증자|전환사채|신주인수권부사채|교환사채|전환청구권|감자\s*결정|무상감자|"
                  r"소송|계약\s*해지|매출액\s*또는\s*손익구조|영업정지|최대주주\s*변경")


def classify(report_nm: str):
    if CRIT.search(report_nm):
        return "crit"
    if WARN.search(report_nm):
        return "warn"
    return None


def annotate_tickers(tickers, days: int = 30, asof: str | None = None) -> dict:
    """{ticker: {"crit": [...], "warn": [...]}} — 위험 공시 있는 종목만 반환.

    각 항목: "MM-DD 보고서명(축약)". 미상장/매핑실패 종목은 건너뜀.
    """
    cmap = corp_code_map()
    end = asof or dt.date.today().strftime("%Y%m%d")
    bgn = (dt.datetime.strptime(end, "%Y%m%d") - dt.timedelta(days=days)).strftime("%Y%m%d")
    out = {}
    for tk in dict.fromkeys(tickers):          # 중복 제거, 순서 유지
        corp = cmap.get(str(tk).zfill(6))
        if not corp:
            continue
        try:
            items = disclosures(corp, bgn, end)
        except Exception as e:
            print(f"  ! DART 조회 실패 {tk}: {e}", file=sys.stderr)
            continue
        crit, warn = [], []
        for it in items:
            kind = classify(it["report_nm"])
            if not kind:
                continue
            label = f"{it['rcept_dt'][4:6]}-{it['rcept_dt'][6:8]} {it['report_nm'][:30]}"
            (crit if kind == "crit" else warn).append(label)
        if crit or warn:
            out[str(tk).zfill(6)] = {"crit": crit[:3], "warn": warn[:3]}
    return out
