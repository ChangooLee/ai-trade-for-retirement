"""공시 기반 악재 필터 — 매수 후보의 최근 공시를 위험도 분류해 화면 경고/매수 제외.

배경: PIT 백테스트에서 큰 꼬리손실(−30%~전손)의 상당수가 공시 동반 이벤트
(상장적격성·감사의견·유상증자 등)였음. LLM 없이 보고서명 패턴으로 분류.

★분류 3단(설계 워크플로 2026-06-16): 정규화 → 네거티브/회복 레이어(먼저) → CRIT → WARN.
  정규화: 선행 정정괄호 [기재정정]/[첨부정정] 제거 · 가운뎃점(ㆍ U+318D, · U+00B7) 제거 · 공백 제거.
  네거티브 레이어가 핵심 수정: 거래정지'해제'/불성실공시'미지정'/전환청구권'행사'/공급계약'체결'/'무상증자'
  같은 회복·호재성 공시가 악재로 오분류(sign-inversion)되던 ~17건 FP를 차단.

분류:
  crit  상장폐지·거래정지급 — 매수 제외 권고(상장적격성·감사의견거절·관리종목지정·회생/파산·
        횡령·배임·완전자본잠식·불성실공시지정·매매거래정지·사채원리금미지급)
  warn  희석·수급 악재 — 표시만(유상증자·CB/BW발행·감자·단일판매공급계약해지·최대주주변경·소송제기 등)
"""
from __future__ import annotations

import datetime as dt
import re
import sys

from app.dart.client import corp_code_map, disclosures

# 정규화: 선행 정정괄호 제거 → 가운뎃점/공백 제거 후 매칭(모든 패턴은 점·공백 없는 형태 가정)
_BRACKET = re.compile(r"^(?:\[[^\]]*\])+")
_DOTS = ("ㆍ", "·", "‧", "・", "•")


def normalize(report_nm: str) -> str:
    s = report_nm or ""
    s = _BRACKET.sub("", s)                 # 선행 [기재정정][첨부정정] 등 제거 후 본문 재분류
    for d in _DOTS:
        s = s.replace(d, "")                # 가운뎃점 제거 → 횡령ㆍ배임 / 횡령·배임 / 횡령배임 동일화
    s = re.sub(r"\s+", "", s)               # 공백 제거
    return s


# STEP 0 — 네거티브/회복 레이어(먼저 평가, 매치 시 None).
#  호재성 토큰: 이미 반영된/긍정 이벤트(청구권행사·만기전사채취득·발행결과·전환가액조정·자사주취득·계약'체결'·무상증자)
NEG_BULLISH = re.compile(
    r"청구권행사|만기전사채취득|발행결과|증권발행결과|전환가액[^/]{0,4}조정|"
    r"자기주식취득|자기주식[^/]{0,4}신탁|단일판매[^/]{0,2}공급계약체결|공급계약체결|무상증자")
#  회복 동사: 지정/정지 등의 해제·종결. 단 '계약해제'(계약 종료=악재)는 제외(lookbehind), 하드악재 동반 시 미적용.
NEG_RECOVERY = re.compile(r"해소|종결|취소|중단|재개|미지정|대상제외|기각|반려|(?<!계약)해제")
HARD_STEM = re.compile(r"상장폐지|회생|파산|횡령|배임|자본잠식|부도")

# STEP 1 — CRIT (매수 제외). 점·공백 제거된 보고서명 기준. 각 항목에 회복 음성 lookahead 보강.
CRIT = re.compile(
    r"상장폐지(?!.*(취소|이의))|"
    r"(상장적격성|실질심사|기업심사위원회)(?!.*(대상제외|대상결정기한|제외결정))|"
    r"관리종목지정(?!해제)|관리종목.{0,3}사유발생|"
    r"(의견거절|감사의견부적정|감사의견한정|비적정|범위제한|내부회계.{0,6}비적정)|"
    r"(회생절차개시|회생절차.{0,3}신청|파산|부도발생|당좌거래정지|해산사유발생)(?!.*종결)|"
    r"(횡령|배임)(?!.*(무혐의|기각))|"
    r"(완전자본잠식|자본전액잠식|자본잠식률)|"
    r"불성실공시법인지정(?!.*미지정)|공시불이행|공시번복|"
    r"(주권매매거래정지|매매거래정지)(?!.*(해제|재개))|"
    r"(사채.{0,4}원리금.{0,4}미지급|원리금미지급)|"
    r"(조회공시요구|시황변동).{0,12}(풍문|보도).{0,20}(횡령|배임|상장폐지|부도)")

# STEP 2 — WARN (표시만, 매수 제외 안 함).
WARN = re.compile(
    r"(유상증자결정|주주배정|일반공모증자|일반공모)(?!.*제3자배정)|"
    r"전환사채권발행결정|전환사채발행|교환사채권발행결정|"
    r"신주인수권부사채권발행결정|신주인수권부사채발행|"
    r"(무상감자|감자결정|감자완료|주식병합)(?!.*(해제|변경상장))|"
    r"단일판매공급계약해지|단일판매공급계약해제|공급계약해지|계약해제|"
    r"(최대주주변경|경영권변경)(?!.*(해제|취소))|"
    r"영업정지|영업양도결정|"
    r"(채권은행.{0,6}관리절차개시|워크아웃)|"
    r"소송등의제기|조회공시요구")


def classify(report_nm: str):
    """보고서명 → 'crit' | 'warn' | None. 네거티브/회복 레이어를 먼저 적용."""
    s = normalize(report_nm)
    if not s:
        return None
    if NEG_BULLISH.search(s):
        return None
    if NEG_RECOVERY.search(s) and not HARD_STEM.search(s):
        return None
    if CRIT.search(s):
        return "crit"
    if WARN.search(s):
        return "warn"
    return None


def annotate_tickers(tickers, days: int = 30, asof: str | None = None) -> dict:
    """{ticker: {"crit": [...], "warn": [...]}} — 위험 공시 있는 종목만 반환.

    각 항목: "MM-DD 보고서명(축약)". 미상장/매핑실패 종목은 건너뜀.
    rcept_dt <= asof 행만 사용(룩어헤드 방지 — list.json은 bgn~end로 이미 제한되나 재확인).
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
            if it["rcept_dt"] and it["rcept_dt"] > end:   # 룩어헤드 방지
                continue
            kind = classify(it["report_nm"])
            if not kind:
                continue
            label = f"{it['rcept_dt'][4:6]}-{it['rcept_dt'][6:8]} {it['report_nm'][:30]}"
            (crit if kind == "crit" else warn).append(label)
        if crit or warn:
            out[str(tk).zfill(6)] = {"crit": crit[:3], "warn": warn[:3]}
    return out


def crit_tickers(flags: dict) -> set:
    """annotate_tickers 결과에서 crit 공시가 있는 종목코드 집합(매수 제외용)."""
    return {tk for tk, f in flags.items() if f.get("crit")}
