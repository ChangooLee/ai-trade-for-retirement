# 아키텍처

## 데이터 (3계층)
| 패널 | 파일 | 원천 | 용도 |
|---|---|---|---|
| 상위 유니버스 | daily_ohlcv.parquet | pykrx(수정주가, 10y) | 화면 신호·차트(정밀) |
| 전종목 | broad_ohlcv.parquet | KRX 공식(원시가, 300d) | 유니버스 밖 분석·차트 |
| 시점별(PIT) | pit_ohlcv*.parquet | KRX 공식(상폐 포함, 10y) | **백테스트 신뢰 기준** |
+ index_ohlcv(지수 주봉, D4 국면) + DART corp_code/공시(악재 필터)

## 신호
- 모멘텀: F리더(RS≥85·52주고≥80%·정배열) ∩ 20주선 눌림(완성주) — 재검증 중
- TDA: Takens→VR H1 지속성(풍경노름·엔트로피·Wasserstein) — 매도/리스크 신호
- 오버나이트: 거래량×3+양봉 → 1박 (Risk-Off 국면 한정 net+)
- D4: 지수 40주선+변동성 분위 → 목표노출 100/70/40%
- DART: 후보 종목 최근 30일 공시 → crit(상폐위험)/warn(희석) 배지

## 화면
build_webapp이 모든 신호·통계(빌드 시 재계산)·차트 데이터를 단일 HTML(JSON 임베드)로 생성.
보유 포트폴리오는 브라우저 localStorage(서버 무전송). nginx alias + no-cache + gzip.
