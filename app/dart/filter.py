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

from app.dart.client import corp_code_map, disclosures, structured_terminal_events

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
    # ★채권/채무 발행·재무이슈(가시성 — 부채/재무상태에 영향) 전부 WARN(앰버)로 강조★
    r"회사채발행|무보증사채|사채권발행|사채발행결정|단기사채발행|채무증권발행|"
    r"신종자본증권|조건부자본증권|전자단기사채|기업어음증권|미상환(사채|증권|잔액)|"
    r"매출액또는손익구조|손익구조30|영업손실|당기순손실|차입금|담보제공|채무보증|"
    r"(무상감자|감자결정|감자완료|주식병합)(?!.*(해제|변경상장))|"
    r"단일판매공급계약해지|단일판매공급계약해제|공급계약해지|계약해제|"
    r"(최대주주변경|경영권변경)(?!.*(해제|취소))|"
    r"영업정지|영업양도결정|"
    r"(채권은행.{0,6}관리절차개시|워크아웃)|"
    r"소송등의제기|조회공시요구")


# TERMINAL — CRIT의 부분집합 중 '보유 불가' 터미널급(상식적 자동 제외 대상).
#  제외(crit 표시는 유지하되 터미널 아님): 횡령·배임(혐의·노이즈), 불성실공시지정, 관리종목지정(회복가능), 자본잠식률(부분).
TERMINAL = re.compile(
    r"상장폐지(?!.*(취소|이의))|"
    r"(상장적격성|실질심사|기업심사위원회)(?!.*(대상제외|대상결정기한|제외결정))|"
    r"(주권매매거래정지|매매거래정지)(?!.*(해제|재개))|"
    r"(의견거절|감사의견부적정|감사의견한정|비적정|범위제한|내부회계.{0,6}비적정)|"
    r"(사채.{0,4}원리금.{0,4}미지급|원리금미지급)|"
    r"(회생절차개시|회생절차.{0,3}신청|파산|부도발생|당좌거래정지|해산사유발생)(?!.*종결)|"
    r"(완전자본잠식|자본전액잠식)")
# 제3자(출자/피보증/타법인) — 그 법인의 부도/회생이지 자기 보통주 distress 아님(자기 distress는 구조화 DS005가 잡음)
_OTHER_CO = re.compile(r"출자법인|타법인|관계회사|종속회사|관계기업|피보증|담보법인")


def is_terminal(report_nm: str) -> bool:
    """터미널급(자동 매수제외 대상) 제목 여부. ★우선주·제3자법인 한정 시장조치는 보통주 distress 아님(FP) → 제외.★
    부도/영업정지/회생/해산/채권관리는 구조화 DS005(client.structured_terminal_events)가 자기회사만 깨끗하게 잡음 —
    이 제목 경로는 list.json의 KRX 시장조치(상폐/정지/실질심사)·감사의견 보완용."""
    if classify(report_nm) != "crit":
        return False
    s = normalize(report_nm)
    if "우선주" in s and "보통주" not in s:        # 우선주 한정 상폐/정지 ≠ 보통주(예: 'DB하이텍1우선주 상장폐지' FP)
        return False
    if _OTHER_CO.search(s):                         # 출자/피보증/타법인의 회생·파산 등(FP)
        return False
    return bool(TERMINAL.search(s))


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


def annotate_tickers(tickers, days: int = 30, asof: str | None = None, structured: bool = False) -> dict:
    """{ticker: {"crit":[...], "warn":[...], "terminal":[...]}} — 위험 공시 있는 종목만 반환.

    각 항목: "MM-DD 사유(축약)". 미상장/매핑실패 종목은 건너뜀. rcept_dt <= asof 행만(룩어헤드 방지).
    structured=True: 부도/영업정지/회생/해산/채권관리를 DS005 구조화 엔드포인트로도 조회해 terminal에 병합
      (자기회사만·제목파싱 불요 — 우선주/피보증법인 FP 없음). 라이브 매수제외 경로에서 사용.
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
        crit, warn, terminal = [], [], []
        for it in items:
            if it["rcept_dt"] and it["rcept_dt"] > end:   # 룩어헤드 방지
                continue
            kind = classify(it["report_nm"])
            if not kind:
                continue
            label = f"{it['rcept_dt'][4:6]}-{it['rcept_dt'][6:8]} {it['report_nm'][:30]}"
            if kind == "crit":
                crit.append(label)
                if is_terminal(it["report_nm"]):          # 제목 경로(상폐/정지/실질심사/감사의견) — 우선주·제3자 가드 적용
                    terminal.append(label)
            else:
                warn.append(label)
        if structured:                                    # 구조화 DS005(부도/영업정지/회생/해산/채권관리 — 자기회사 깨끗)
            try:
                for ev in structured_terminal_events(corp, bgn, end):
                    if ev["date"] <= end:
                        terminal.append(f"{ev['date'][4:6]}-{ev['date'][6:8]} {ev['event']}(DS005)")
            except Exception as e:
                print(f"  ! DART 구조화 조회 실패 {tk}: {e}", file=sys.stderr)
        if crit or warn or terminal:
            out[str(tk).zfill(6)] = {"crit": crit[:3], "warn": warn[:3], "terminal": terminal[:3]}
    return out


def terminal_events(corp_code: str, bgn_de: str, end_de: str) -> list[str]:
    """하이브리드 terminal 탐지 → 정렬된 rcept_dt 리스트.
    (1) DS005 구조화(부도/영업정지/회생/해산/채권관리 — 자기회사) (2) list.json 제목 is_terminal(상폐/정지/실질심사/감사의견)."""
    dates = {ev["date"] for ev in structured_terminal_events(corp_code, bgn_de, end_de)}
    for it in disclosures(corp_code, bgn_de, end_de):
        if it.get("rcept_dt") and it["rcept_dt"] <= end_de and is_terminal(it["report_nm"]):
            dates.add(it["rcept_dt"])
    return sorted(dates)


from app.dart.client import (financial_accounts, financial_indicators,  # noqa: E402
                             dilution_events, major_holders, insider_trades, audit_opinion)

_PERIODIC = re.compile(r"(사업보고서|반기보고서|분기보고서)")
# 정기·루틴 공시(이벤트성 아님) — 최근공시 목록에서 제외해 의미있는 뉴스만 남김.
#  지분 루틴(소유상황·대량보유·변동신고)은 별도 '지분' 섹션서 처리하므로 목록서 빼도 신호 손실 없음.
_ROUTINE_DISCL = re.compile(
    r"사업보고서|반기보고서|분기보고서|기업지배구조보고서|대규모기업집단현황|"
    r"소유상황보고서|소유주식변동신고서|대량보유상황보고서|"
    r"임원.{0,2}주요주주|사외이사.{0,4}(현황|선임|해임)|"
    r"주주총회소집|정기주주총회|의결권대리행사|감사보고서제출|결산실적공시예고|기업설명회|IR")
# 유지(의미있는 뉴스): 영업(잠정)실적·기업가치제고계획·매출또는손익구조변경·단일판매공급계약·증자/CB·합병 등은 제외 대상 아님
_PERIOD_DT = re.compile(r"\(?(\d{4})[.\-/](\d{2})\)?")     # (2024.12) / 2025.03
_MO2REPRT = {3: "11013", 6: "11012", 9: "11014", 12: "11011"}   # 1Q/반기/3Q/사업


def latest_report(corp_code: str, asof: str):
    """asof(YYYYMMDD) 이전 가장 최근 정기보고서 → (bsns_year, reprt_code) | None. ★룩어헤드: rcept_dt<=asof만★."""
    bgn = f"{int(asof[:4]) - 2}{asof[4:]}"                 # 2년 전부터(직전 정기보고서 확보 충분)
    best = None
    for it in disclosures(corp_code, bgn, asof):
        nm, rd = it["report_nm"], it["rcept_dt"]
        if not rd or rd > asof or not _PERIODIC.search(nm):
            continue
        m = _PERIOD_DT.search(nm)
        reprt = _MO2REPRT.get(int(m.group(2))) if m else None
        if reprt and (best is None or rd > best[2]):
            best = (m.group(1), reprt, rd)
    return (best[0], best[1]) if best else None


def financial_distress(corp_code: str, bsns_year: str, reprt_code: str) -> dict:
    """재무 부실 판정 — 자본잠식(완전=crit·부분=warn)·부채비율≥400%·유동비율<50%(warn). DS003 재무제표+안정성비율."""
    acc = financial_accounts(corp_code, bsns_year, reprt_code)
    ind = financial_indicators(corp_code, bsns_year, reprt_code, "M220000")
    cap_total, cap_stock = acc.get("자본총계"), acc.get("자본금")
    debt, curr = ind.get("부채비율"), ind.get("유동비율")
    revenue, op_income = acc.get("매출액"), acc.get("영업이익")
    flags, level = [], None
    if cap_total is not None and cap_total <= 0:
        flags.append("완전자본잠식"); level = "crit"
    elif cap_total is not None and cap_stock and cap_total < cap_stock:
        flags.append("부분자본잠식"); level = level or "warn"
    if debt is not None and debt >= 400:
        flags.append(f"부채비율{debt:.0f}%"); level = level or "warn"
    if curr is not None and 0 < curr < 50:
        flags.append(f"유동비율{curr:.0f}%"); level = level or "warn"
    return {"flags": flags, "level": level, "cap_total": cap_total, "cap_stock": cap_stock,
            "debt_ratio": debt, "curr_ratio": curr, "revenue": revenue, "op_income": op_income,
            "op_loss": (op_income is not None and op_income < 0), "bsns_year": bsns_year, "reprt_code": reprt_code}


def company_dart_profile(ticker: str, asof: str | None = None, recent_n: int = 10, lookback_days: int = 365) -> dict | None:
    """종목 1개 DART 종합 프로파일 — 재무부실(1)·희석(2)·지분(3)·공급계약(4)·최근공시 N건.
    ★asof 이전(rcept_dt<=asof)만 — 룩어헤드 차단.★ 미상장/매핑실패 → None. (라이브 /api/dart·화면용)"""
    cmap = corp_code_map()
    corp = cmap.get(str(ticker).zfill(6))
    if not corp:
        return None
    end = asof or dt.date.today().strftime("%Y%m%d")
    bgn = (dt.datetime.strptime(end, "%Y%m%d") - dt.timedelta(days=lookback_days)).strftime("%Y%m%d")
    p = {"ticker": str(ticker).zfill(6)}
    try:                                                   # 1순위 재무부실 + 감사의견(같은 보고서)
        rep = latest_report(corp, end)
        if rep:
            p["distress"] = financial_distress(corp, rep[0], rep[1])
            try:
                p["audit"] = audit_opinion(corp, rep[0], "11011")   # 감사의견은 연간 사업보고서 기준
            except Exception:
                p["audit"] = None
        else:
            p["distress"], p["audit"] = None, None
    except Exception:
        p["distress"], p["audit"] = None, None
    try:                                                   # 2순위 희석(유증/CB)
        p["dilution"] = [d for d in dilution_events(corp, bgn, end) if d["date"] <= end][:5]
    except Exception:
        p["dilution"] = []
    try:                                                   # 3순위 대량보유·임원매도
        p["major_holders"] = [h for h in major_holders(corp) if h["date"] <= end][:5]
        p["insiders"] = [h for h in insider_trades(corp) if h["date"] <= end][:5]
    except Exception:
        p["major_holders"], p["insiders"] = [], []
    try:                                                   # 4순위 공급계약 + 자사주매입 + 최근공시(정기·루틴 제외)
        items = [it for it in disclosures(corp, bgn, end) if it["rcept_dt"] and it["rcept_dt"] <= end]
        def _nm(it):
            return it["report_nm"].replace(" ", "")
        p["contracts"] = [{"date": it["rcept_dt"], "title": it["report_nm"]}
                          for it in items if "공급계약" in _nm(it) and "체결" in _nm(it)][:5]
        p["buyback"] = [{"date": it["rcept_dt"], "title": it["report_nm"]}      # 자사주 매입/신탁(긍정)
                        for it in items if "자기주식" in _nm(it) and ("취득" in _nm(it) or "신탁" in _nm(it)) and "처분" not in _nm(it)][:3]
        meaningful = [it for it in items if not _ROUTINE_DISCL.search(_nm(it))]  # ★정기·루틴 공시 제외★
        p["recent"] = [{"date": it["rcept_dt"], "title": it["report_nm"],
                        "kind": ("terminal" if is_terminal(it["report_nm"]) else classify(it["report_nm"]))}
                       for it in meaningful[:recent_n]]
    except Exception:
        p["contracts"], p["buyback"], p["recent"] = [], [], []
    return p


def crit_tickers(flags: dict) -> set:
    """annotate_tickers 결과에서 crit 공시가 있는 종목코드 집합."""
    return {tk for tk, f in flags.items() if f.get("crit")}


def terminal_tickers(flags: dict) -> set:
    """터미널급(상폐/정지/감사거절/부도/완전잠식) 공시 종목 — 자동 매수제외 대상."""
    return {tk for tk, f in flags.items() if f.get("terminal")}
