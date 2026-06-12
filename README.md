# ai-trade-for-retirement

한국 주식(KRX) **프로그램 매매 시스템**. 검증된 신호로 데일리 트레이딩 화면을 자동 생성하고,
모든 전략은 **생존편향 없는(point-in-time) 백테스트**를 통과해야만 실행 후보가 된다.

- 운영 화면: **https://sourceport.ai/trading** (매 거래일 08:17경 자동 갱신)
- 데이터: KRX 공식 OpenAPI(전종목·지수·시점별 패널) + pykrx(수정주가) + **OpenDART(공시 위험 필터)**
- 원칙: **돈을 잃지 않는 것이 1순위.** 검증 안 된 수익 주장 금지(이 저장소의 모든 성과 수치는 출처 백테스트가 명시됨)

## 왜 이 프로젝트인가
이전 프로젝트(mcp-krx)에서 전략 백테스트가 **+531%(연 22.6%)** 를 보였으나, 상장폐지 종목까지
포함한 **PIT 재검증에서 −20.4%** 로 뒤집혔다(생존편향). 원인은 전략이 아니라 '거래대금 상위' 유니버스
자체가 −49% 침몰하는 투기 풀이었다는 것 — **시가총액 유니버스로 바꾸자 PIT +67.7%(CAGR +5.9%)로 알파가
복원**됐다(결정 0005, 현재 라이브 적용). 이 저장소는 그 교훈 위에 세워졌다:
1. 모든 전략은 PIT 패널(`pit_ohlcv`)로 재검증 후 채택
2. 화면의 모든 수치는 빌드 시 원데이터에서 재계산(하드코딩 금지)
3. 공시(DART) 기반 악재 필터로 꼬리위험(상폐·희석) 사전 차단

## 구조
```
app/
  data/       krx_api(공식 API 클라이언트) · fetchers(수집) · krx_loader · calendar · validators
  indicators/ daily · weekly · regime(D4) · leader(F리더) · pullback(20주선) · tda(위상수학)
  portfolio/  sizing · ledger · orders
  dart/       client(OpenDART) · filter(악재 분류: crit=상폐위험 / warn=희석·수급)
  render/     templates/trade_app.html(화면) · sectors
  batch/      update_data(증분) · build_broad(전종목) · build_pit(시점별 패널) · build_webapp(화면 생성)
backtest/     portfolio · pit_portfolio(생존편향 제거) · overnight_deployed · shortterm v1/v2 · overlap · tda_exit
config/       strategy.yaml (전략 파라미터 — 단일 진실원)
docs/         architecture · validation-status(검증 현황) · quality-gates · runbooks · decisions/
scripts/      server_daily_batch.sh(매일 08:10 cron) · deploy
state/        실행 산출물(빌드 결과·리포트) — git 미추적
```

## 빠른 시작
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # KRX_AUTH_KEY, OPENDART_API_KEY 입력
.venv/bin/python -m app.batch.update_data          # 데이터 증분 갱신
.venv/bin/python -m app.batch.build_broad          # 전종목 패널
.venv/bin/python -m app.batch.build_webapp --out state/trade.html
python3 -m http.server 8776                        # → http://127.0.0.1:8776/state/trade.html
```

## 전략 검증 현황 (정직 고지)
| 전략 | 생존 패널 | **PIT(신뢰 기준)** | 상태 |
|---|---|---|---|
| 모멘텀·추세(F리더∩20주선 눌림, **시총 top300**) | +531% | **+67.7%** (CAGR +5.9%, MDD −43%, 풀 +4.2% 대비 +63.5%p) | ✅ 라이브 적용(시총 유니버스, 결정 0005) |
| └ 이전 거래대금 유니버스 | +531% | −20.4% (풀 −49% 투기 드리프트) | ❌ 폐기(생존편향이 가렸던 풀 침몰) |
| 복합(장기80%+단타20%) + 월손실 −3% 서킷브레이커 | — | **CAGR +8.3%, MDD −25%, 최악월 −6.3%** | ✅ 운영 구성(결정 0004) |
| TDA 매수 | 알파 없음 | — | 매도/리스크 신호로만 사용 |
| 오버나이트 1박(거래량 폭발) | CAGR +1.3% | Risk-Off만 net+ | 보조(≤20% 분산용, 단독 엣지≈0) |
| DART 악재 필터 | — | — | 분류기 검증 통과, 효과 백테스트 예정 |

상세: [`docs/validation-status.md`](docs/validation-status.md) · 검증 기준: [`docs/quality-gates.md`](docs/quality-gates.md)

## 일일 운영
서버(개발서버) cron `10 8 * * 1-6` → `scripts/server_daily_batch.sh`:
증분 데이터 → 전종목 패널 → 화면 생성(`/var/www/leaders/index.html`). 운영 절차·장애 복구는
[`docs/runbook-daily-ops.md`](docs/runbook-daily-ops.md), [`docs/runbook-recovery.md`](docs/runbook-recovery.md).

## 에이전트로 작업하기
Claude Code 등 코딩 에이전트는 **[`AGENTS.md`](AGENTS.md)** 를 먼저 읽는다 — 검증 규칙·금지 사항·
정의된 완료 조건이 거기에 있다. 사람 운영자는 이 README와 `docs/runbook-*`를 따른다.
구조적 결정은 `docs/decisions/`에 기록한다.
