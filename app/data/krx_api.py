"""KRX 공식 Open API 클라이언트 (data-dbg.krx.co.kr/svc/apis).

pykrx(스크래핑)와 별개인 KRX 정보데이터시스템 **공식** Open API 래퍼.
- 베이스: https://data-dbg.krx.co.kr/svc/apis
- 메서드: GET
- 인증: 발급키를 AUTH_KEY 로 전달 (쿼리 파라미터 + 헤더 둘 다 — 어느 쪽이든 수용)
- 파라미터: basDd=YYYYMMDD (기준일자)
- 응답: JSON, 데이터는 "OutBlock_1" 배열
- 제한: 하루 10,000콜, 데이터 2010년~, 비상업적 사용

⚠️ 인증키 발급과 **별개로** 데이터셋(서비스)별 이용신청·승인이 필요하다.
   미승인 서비스를 호출하면 401 {"respCode":"401","respMsg":"Unauthorized API Call"} 가 온다.
   openapi.krx.co.kr → 로그인 → 서비스 이용 → 원하는 API 이용신청 → 승인(보통 1일).
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

import requests

logger = logging.getLogger("mcp-pykrx")

KRX_BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"

# 공식 엔드포인트 카탈로그: 별칭(leaf) -> (전체경로, 카테고리, 한글명)
# 31개 전 서비스. call(별칭 또는 "cat/leaf") 모두 허용.
KRX_ENDPOINTS: dict[str, tuple[str, str, str]] = {
    # 지수 (idx)
    "krx_dd_trd": ("idx/krx_dd_trd", "idx", "KRX 시리즈 일별시세"),
    "kospi_dd_trd": ("idx/kospi_dd_trd", "idx", "KOSPI 시리즈 일별시세"),
    "kosdaq_dd_trd": ("idx/kosdaq_dd_trd", "idx", "KOSDAQ 시리즈 일별시세"),
    "bon_dd_trd": ("idx/bon_dd_trd", "idx", "채권지수 시세"),
    "drvprod_dd_trd": ("idx/drvprod_dd_trd", "idx", "파생상품지수 시세"),
    # 주식 (sto)
    "stk_bydd_trd": ("sto/stk_bydd_trd", "sto", "유가증권(KOSPI) 일별매매정보"),
    "ksq_bydd_trd": ("sto/ksq_bydd_trd", "sto", "코스닥 일별매매정보"),
    "knx_bydd_trd": ("sto/knx_bydd_trd", "sto", "코넥스 일별매매정보"),
    "sw_bydd_trd": ("sto/sw_bydd_trd", "sto", "신주인수권증권 일별매매정보"),
    "sr_bydd_trd": ("sto/sr_bydd_trd", "sto", "신주인수권증서 일별매매정보"),
    "stk_isu_base_info": ("sto/stk_isu_base_info", "sto", "유가증권 종목기본정보"),
    "ksq_isu_base_info": ("sto/ksq_isu_base_info", "sto", "코스닥 종목기본정보"),
    "knx_isu_base_info": ("sto/knx_isu_base_info", "sto", "코넥스 종목기본정보"),
    # ETP (etp)
    "etf_bydd_trd": ("etp/etf_bydd_trd", "etp", "ETF 일별매매정보"),
    "etn_bydd_trd": ("etp/etn_bydd_trd", "etp", "ETN 일별매매정보"),
    "elw_bydd_trd": ("etp/elw_bydd_trd", "etp", "ELW 일별매매정보"),
    # 채권 (bon)
    "kts_bydd_trd": ("bon/kts_bydd_trd", "bon", "국채전문유통시장 일별매매정보"),
    "bnd_bydd_trd": ("bon/bnd_bydd_trd", "bon", "일반채권시장 일별매매정보"),
    "smb_bydd_trd": ("bon/smb_bydd_trd", "bon", "소액채권시장 일별매매정보"),
    # 파생 (drv)
    "fut_bydd_trd": ("drv/fut_bydd_trd", "drv", "선물 일별매매정보(주식선물外)"),
    "eqsfu_stk_bydd_trd": ("drv/eqsfu_stk_bydd_trd", "drv", "주식선물(유가) 일별매매정보"),
    "eqkfu_ksq_bydd_trd": ("drv/eqkfu_ksq_bydd_trd", "drv", "주식선물(코스닥) 일별매매정보"),
    "opt_bydd_trd": ("drv/opt_bydd_trd", "drv", "옵션 일별매매정보(주식옵션外)"),
    "eqsop_bydd_trd": ("drv/eqsop_bydd_trd", "drv", "주식옵션(유가) 일별매매정보"),
    "eqkop_bydd_trd": ("drv/eqkop_bydd_trd", "drv", "주식옵션(코스닥) 일별매매정보"),
    # 일반상품 (gen)
    "oil_bydd_trd": ("gen/oil_bydd_trd", "gen", "석유시장 일별매매정보"),
    "gold_bydd_trd": ("gen/gold_bydd_trd", "gen", "금시장 일별매매정보"),
    "ets_bydd_trd": ("gen/ets_bydd_trd", "gen", "배출권시장 일별매매정보"),
    # ESG (esg)
    "sri_bond_info": ("esg/sri_bond_info", "esg", "사회책임투자채권 정보"),
    "esg_etp_info": ("esg/esg_etp_info", "esg", "ESG 증권상품"),
    "esg_index_info": ("esg/esg_index_info", "esg", "ESG 지수"),
}


class KRXOpenAPIError(Exception):
    """KRX 공식 API 일반 오류."""


class KRXAuthError(KRXOpenAPIError):
    """인증/권한 오류 (키 미설정 또는 서비스 미승인 401)."""


def _resolve_path(endpoint: str) -> str:
    """별칭(leaf) 또는 'cat/leaf' 전체경로를 전체경로로 정규화."""
    if endpoint in KRX_ENDPOINTS:
        return KRX_ENDPOINTS[endpoint][0]
    if "/" in endpoint:  # 이미 cat/leaf 형태
        return endpoint
    raise KRXOpenAPIError(f"알 수 없는 엔드포인트: {endpoint!r}. "
                          f"사용 가능: {', '.join(sorted(KRX_ENDPOINTS))}")


class KRXOpenAPIClient:
    """KRX 공식 Open API 클라이언트."""

    def __init__(
        self,
        auth_key: Optional[str] = None,
        base_url: str = KRX_BASE_URL,
        timeout: float = 30.0,  # 옵션 등 대용량 엔드포인트는 더 크게 (예: 60)
        delay: float = 0.2,
    ):
        # 키 미지정 시 환경변수에서 로드 (양끝 공백/개행 제거 필수)
        self.auth_key = (auth_key if auth_key is not None else os.getenv("KRX_AUTH_KEY", "")).strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.delay = delay
        self._session = requests.Session()

    @property
    def has_key(self) -> bool:
        return bool(self.auth_key)

    def call(self, endpoint: str, bas_dd: Optional[str] = None, **extra: Any) -> list[dict]:
        """엔드포인트 호출 → OutBlock_1 레코드 리스트 반환.

        Args:
            endpoint: 별칭("stk_bydd_trd") 또는 전체경로("sto/stk_bydd_trd")
            bas_dd: 기준일자 YYYYMMDD
            **extra: 추가 쿼리 파라미터
        """
        if not self.has_key:
            raise KRXAuthError(
                "KRX_AUTH_KEY가 설정되지 않았습니다. .env에 KRX_AUTH_KEY=... 를 넣으세요."
            )
        path = _resolve_path(endpoint)
        if bas_dd is not None and not re.match(r"^\d{8}$", str(bas_dd)):
            raise ValueError(f"basDd는 YYYYMMDD 형식이어야 합니다: {bas_dd!r}")

        params: dict[str, Any] = {"AUTH_KEY": self.auth_key}
        if bas_dd is not None:
            params["basDd"] = str(bas_dd)
        params.update({k: v for k, v in extra.items() if v is not None})
        # 헤더로도 전달 (서버가 헤더/쿼리 어느 쪽을 읽든 대응)
        headers = {"AUTH_KEY": self.auth_key, "Accept": "application/json"}
        url = f"{self.base_url}/{path}"

        time.sleep(self.delay)
        try:
            r = self._session.get(url, params=params, headers=headers, timeout=self.timeout)
        except requests.Timeout as e:
            raise KRXOpenAPIError(f"요청 타임아웃({self.timeout}s): {path}") from e
        except requests.RequestException as e:
            raise KRXOpenAPIError(f"네트워크 오류: {e}") from e

        if r.status_code == 401:
            raise KRXAuthError(
                f"401 Unauthorized — 키가 '{path}' 서비스에 이용신청/승인되지 않았습니다. "
                "openapi.krx.co.kr → 로그인 → '서비스 이용'에서 해당 API 이용신청 후 "
                "승인(보통 1일)되면 동작합니다. (키 발급과 서비스 신청은 별개)"
            )
        if r.status_code == 404:
            raise KRXOpenAPIError(f"404 — 잘못된 엔드포인트 경로: '{path}'")
        if r.status_code >= 500:
            raise KRXOpenAPIError(f"{r.status_code} — KRX 서버 오류: {path}")
        try:
            data = r.json()
        except ValueError as e:
            raise KRXOpenAPIError(f"JSON 파싱 실패(HTTP {r.status_code}): {r.text[:200]}") from e

        # 오류 바디가 200으로 올 수도 있어 방어적으로 확인
        if isinstance(data, dict) and data.get("respCode") not in (None, "00", "000"):
            if str(data.get("respCode")) == "401":
                raise KRXAuthError(f"{data.get('respCode')} {data.get('respMsg')} — {path}")
        return data.get("OutBlock_1", []) if isinstance(data, dict) else []

    def call_df(self, endpoint: str, bas_dd: Optional[str] = None, **extra: Any):
        """call() 결과를 pandas DataFrame으로 반환 (pandas 미설치 시 list 반환)."""
        rows = self.call(endpoint, bas_dd, **extra)
        try:
            import pandas as pd
            return pd.DataFrame(rows)
        except ImportError:
            return rows

    # ---- 편의 메서드 ----
    def stock_daily_trade(self, bas_dd: str, market: str = "KOSPI"):
        ep = {"KOSPI": "stk_bydd_trd", "KOSDAQ": "ksq_bydd_trd",
              "KONEX": "knx_bydd_trd"}[market.upper()]
        return self.call_df(ep, bas_dd)

    def stock_base_info(self, bas_dd: str, market: str = "KOSPI"):
        ep = {"KOSPI": "stk_isu_base_info", "KOSDAQ": "ksq_isu_base_info",
              "KONEX": "knx_isu_base_info"}[market.upper()]
        return self.call_df(ep, bas_dd)

    def index_daily_trade(self, bas_dd: str, market: str = "KOSPI"):
        ep = {"KRX": "krx_dd_trd", "KOSPI": "kospi_dd_trd",
              "KOSDAQ": "kosdaq_dd_trd"}[market.upper()]
        return self.call_df(ep, bas_dd)

    def etp_daily_trade(self, bas_dd: str, kind: str = "ETF"):
        ep = {"ETF": "etf_bydd_trd", "ETN": "etn_bydd_trd",
              "ELW": "elw_bydd_trd"}[kind.upper()]
        return self.call_df(ep, bas_dd)
