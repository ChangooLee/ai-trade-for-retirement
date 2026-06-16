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
                    rd = x.get("rcept_dt")
                    if rd:
                        out.append({"date": rd, "event": name})
        except Exception:
            continue                                  # 한 엔드포인트 실패가 전체를 막지 않게(fail-open)
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
    return [{"rcept_dt": x.get("rcept_dt", ""), "report_nm": (x.get("report_nm") or "").strip(),
             "rcept_no": x.get("rcept_no", "")} for x in d.get("list", [])]
