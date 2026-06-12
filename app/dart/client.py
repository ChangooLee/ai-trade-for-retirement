"""OpenDART API 클라이언트 — 공시 목록 조회 + 종목코드→고유번호(corp_code) 매핑.

mcp-opendart 서버를 띄우지 않고 REST API만 직접 사용한다(사용자 결정).
키: .env의 OPENDART_API_KEY. 매핑: corpCode.xml(zip)을 1회 내려받아 캐시.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
import xml.etree.ElementTree as ET

import requests

BASE = "https://opendart.fss.or.kr/api"
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


def disclosures(corp_code: str, bgn_de: str, end_de: str, timeout: int = 15) -> list[dict]:
    """기간 내 공시 목록 [{rcept_dt, report_nm, rcept_no}, ...] (최신순)."""
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
