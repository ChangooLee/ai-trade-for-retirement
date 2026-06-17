"""OpenDART API 클라이언트 — 공시 목록 조회 + 종목코드→고유번호(corp_code) 매핑.

mcp-opendart 서버를 띄우지 않고 REST API만 직접 사용한다(사용자 결정).
키: .env의 OPENDART_API_KEY. 매핑: corpCode.xml(zip)을 1회 내려받아 캐시.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET

import requests

BASE = "https://opendart.fss.or.kr/api"
DART_MIN_DATE = "20150101"     # DART 주요사항보고서(DS005) 등 2015년 이후 제공


def _check_range(bgn_de: str, end_de: str, clamp_min: bool = False):
    """★DART 기간 검증 — 잘못된 범위로 조회해 '공시 없음'을 악재 없음으로 오판하는 것 방지.★
    형식(YYYYMMDD)·순서(bgn<=end) 위반은 즉시 ValueError. clamp_min=True면 bgn을 2015 하한으로 보정해 반환."""
    for label, d in (("bgn_de", bgn_de), ("end_de", end_de)):
        if not (isinstance(d, str) and re.fullmatch(r"\d{8}", d)):
            raise ValueError(f"DART 기간 형식 오류({label}=YYYYMMDD 필요): bgn={bgn_de!r} end={end_de!r}")
    if bgn_de > end_de:
        raise ValueError(f"DART 기간 역순(bgn>end): {bgn_de} > {end_de}")
    if clamp_min and bgn_de < DART_MIN_DATE:
        return DART_MIN_DATE, end_de
    return bgn_de, end_de
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORP_CACHE = os.path.join(_REPO, "data/cache/dart_corp_codes.json")


def load_dart_key() -> str:
    key = os.getenv("OPENDART_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.join(_REPO, ".env"), encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("OPENDART_API_KEY="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def corp_code_map(refresh: bool = False) -> dict:
    """{6자리 종목코드: 8자리 corp_code}. corpCode.xml 1회 다운로드 후 캐시."""
    if not refresh and os.path.exists(CORP_CACHE):
        try:
            return json.load(open(CORP_CACHE, encoding="utf-8"))
        except Exception:
            pass
    key = load_dart_key()
    r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": key}, timeout=60)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml)
    out = {}
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        if len(stock) == 6 and stock.isdigit() and corp:
            out[stock] = corp
    os.makedirs(os.path.dirname(CORP_CACHE), exist_ok=True)
    json.dump(out, open(CORP_CACHE, "w", encoding="utf-8"))
    return out


# DS005 주요사항보고서 — 자기 회사 distress 구조화 엔드포인트(제목 정규식 불요·우선주/피보증법인 노이즈 없음)
STRUCTURED_TERMINAL = {
    "부도": "dfOcr.json", "영업정지": "bsnSp.json", "회생절차": "ctrcvsBgrq.json",
    "해산": "dsRsOcr.json", "채권관리절차": "bnkMngtPcbg.json",
}


def structured_terminal_events(corp_code: str, bgn_de: str, end_de: str, timeout: int = 12) -> list[dict]:
    """DS005 구조화 엔드포인트로 자기회사 terminal 이벤트 조회 → [{date, event}]. 빈/오류 엔드포인트는 건너뜀.
    부도·영업정지·회생·해산·채권관리는 거래소 시장조치(상폐/정지)와 달리 DART 주요사항보고서로 정형 제공."""
    bgn_de, end_de = _check_range(bgn_de, end_de, clamp_min=True)   # DS005는 2015+ — 하한 보정 + 형식/순서 검증
    key = load_dart_key()
    out = []
    for name, ep in STRUCTURED_TERMINAL.items():
        try:
            r = requests.get(f"{BASE}/{ep}", params={"crtfc_key": key, "corp_code": corp_code,
                             "bgn_de": bgn_de, "end_de": end_de}, timeout=timeout)
            d = r.json()
            if d.get("status") == "000":
                for x in d.get("list", []):
                    rd = _d8(x.get("rcept_dt"))
                    if rd:
                        out.append({"date": rd, "event": name})
        except Exception:
            continue                                  # 한 엔드포인트 실패가 전체를 막지 않게(fail-open)
    return out


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _d8(v) -> str:
    """DART 날짜 정규화 → YYYYMMDD. ★엔드포인트마다 '20260604' vs '2026-05-22' 혼재 → 통일★
    (혼재 시 'date<=asof' 룩어헤드 비교가 깨짐 — 반드시 정규화)."""
    return re.sub(r"\D", "", str(v or ""))[:8]


def dilution_events(corp_code: str, bgn_de: str, end_de: str, timeout: int = 12) -> list[dict]:
    """DS005 유상증자(piicDecsn)+전환사채(cvbdIsDecsn) 발행규모 → [{date,type,dilution_pct,method}] (희석 정량화·2순위)."""
    _check_range(bgn_de, end_de, clamp_min=True)
    key = load_dart_key()
    out = []
    for ep, typ in (("piicDecsn.json", "유상증자"), ("cvbdIsDecsn.json", "전환사채")):
        try:
            d = requests.get(f"{BASE}/{ep}", params={"crtfc_key": key, "corp_code": corp_code,
                             "bgn_de": bgn_de, "end_de": end_de}, timeout=timeout).json()
            if d.get("status") != "000":
                continue
            for x in d.get("list", []):
                new = _num(x.get("nstk_ostk_cnt")); base = _num(x.get("bfic_tisstk_ostk"))
                pct = (new / base * 100) if (new and base) else None
                out.append({"date": _d8(x.get("rcept_dt")), "type": typ,
                            "dilution_pct": round(pct, 1) if pct else None,
                            "method": (x.get("ic_mthn") or "").strip()[:20]})
        except Exception:
            continue
    return sorted(out, key=lambda e: e["date"], reverse=True)


def major_holders(corp_code: str, timeout: int = 12) -> list[dict]:
    """DS004 대량보유(majorstock, 5%↑) 최신 변동 → [{date,who,rate,change}] (3순위)."""
    key = load_dart_key()
    try:
        d = requests.get(f"{BASE}/majorstock.json", params={"crtfc_key": key, "corp_code": corp_code},
                         timeout=timeout).json()
        if d.get("status") != "000":
            return []
        rows = [{"date": _d8(x.get("rcept_dt")), "who": (x.get("repror") or "").strip(),
                 "rate": _num(x.get("stkrt")), "change": _num(x.get("stkrt_irds"))} for x in d.get("list", [])]
        return sorted(rows, key=lambda e: e["date"], reverse=True)
    except Exception:
        return []


def insider_trades(corp_code: str, timeout: int = 12) -> list[dict]:
    """DS004 임원·주요주주 소유(elestock) 최신 → [{date,who,pos,change}] (3순위, 매도=change<0)."""
    key = load_dart_key()
    try:
        d = requests.get(f"{BASE}/elestock.json", params={"crtfc_key": key, "corp_code": corp_code},
                         timeout=timeout).json()
        if d.get("status") != "000":
            return []
        rows = [{"date": _d8(x.get("rcept_dt")), "who": (x.get("repror") or "").strip(),
                 "pos": (x.get("isu_exctv_ofcps") or "").strip(), "change": _num(x.get("sp_stock_lmp_irds_cnt"))}
                for x in d.get("list", [])]
        return sorted(rows, key=lambda e: e["date"], reverse=True)
    except Exception:
        return []


def _check_report(bsns_year: str, reprt_code: str):
    if not re.fullmatch(r"\d{4}", str(bsns_year)):
        raise ValueError(f"DART 사업연도 형식 오류: {bsns_year!r}")
    if str(reprt_code) not in ("11011", "11012", "11013", "11014"):
        raise ValueError(f"DART 보고서코드 오류(11011/11012/11013/11014): {reprt_code!r}")


def financial_accounts(corp_code, bsns_year, reprt_code, fs_div="OFS", timeout=15) -> dict:
    """DS003 fnlttSinglAcnt — 정기보고서 재무제표 {account_nm: 당기금액(float)}. 자본잠식 판정용(자본총계/자본금)."""
    _check_report(bsns_year, reprt_code)
    r = requests.get(f"{BASE}/fnlttSinglAcnt.json", params={
        "crtfc_key": load_dart_key(), "corp_code": corp_code, "bsns_year": str(bsns_year),
        "reprt_code": str(reprt_code), "fs_div": fs_div}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "000":
        return {}
    out = {}
    for x in d.get("list", []):
        nm = (x.get("account_nm") or "").strip()
        v = str(x.get("thstrm_amount", "")).replace(",", "")
        try:
            out[nm] = float(v)
        except ValueError:
            pass
    return out


def financial_indicators(corp_code, bsns_year, reprt_code, idx_cl_code="M220000", timeout=15) -> dict:
    """DS003 fnlttSinglIndx — 재무비율 {idx_nm: idx_val(float)}. 기본 M220000=안정성(부채비율·유동비율 등)."""
    _check_report(bsns_year, reprt_code)
    r = requests.get(f"{BASE}/fnlttSinglIndx.json", params={
        "crtfc_key": load_dart_key(), "corp_code": corp_code, "bsns_year": str(bsns_year),
        "reprt_code": str(reprt_code), "idx_cl_code": idx_cl_code}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "000":
        return {}
    out = {}
    for x in d.get("list", []):
        nm = (x.get("idx_nm") or "").strip()
        v = x.get("idx_val")
        try:
            out[nm] = float(v) if v not in (None, "", "-") else None
        except (ValueError, TypeError):
            out[nm] = None
    return out


def disclosures(corp_code: str, bgn_de: str, end_de: str, timeout: int = 15) -> list[dict]:
    """기간 내 공시 목록 [{rcept_dt, report_nm, rcept_no}, ...] (최신순)."""
    _check_range(bgn_de, end_de)               # ★기간 형식/순서 검증 — 잘못된 범위 조용한 빈결과 방지★
    key = load_dart_key()
    r = requests.get(f"{BASE}/list.json", params={
        "crtfc_key": key, "corp_code": corp_code,
        "bgn_de": bgn_de, "end_de": end_de, "page_count": 100,
    }, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "000":          # 013 = 조회 결과 없음(정상)
        return []
    return [{"rcept_dt": _d8(x.get("rcept_dt")), "report_nm": (x.get("report_nm") or "").strip(),
             "rcept_no": x.get("rcept_no", "")} for x in d.get("list", [])]
