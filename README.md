# ai-trade-for-retirement

한국 주식(KRX) **퀀트 트레이딩 시스템**. 검증된 신호로 데일리 트레이딩 대시보드를 자동 생성하고,
사용자별 **페이퍼 트레이딩(모의 운용)** 을 매 거래일 전진시킨다. 모든 전략은 상장폐지 종목까지 포함한
**생존편향 없는(point-in-time) 백테스트**를 통과해야만 실행 후보가 된다.

- 운영 화면: **https://sourceport.ai/trading** (매 거래일 08:10 배치로 자동 갱신)
- 현재 단계: **신호 생성 + 페이퍼 트레이딩** — 실주문 집행 코드는 없다(설계상 명시적 승인 전까지 금지).
- 데이터: KRX 공식 OpenAPI(전종목·지수·시점별 패널) + pykrx(수정주가) + **OpenDART(공시 위험 필터)**
- 원칙: **돈을 잃지 않는 것이 1순위.** 검증 안 된 수익 주장 금지 — 이 저장소의 모든 성과 수치는 출처 백테스트가 명시된다.

## 왜 이 프로젝트인가
이전 프로젝트(mcp-krx)에서 전략 백테스트가 **+531%(연 22.6%)** 를 보였으나, 상장폐지 종목까지
포함한 **PIT 재검증에서 −20.4%** 로 뒤집혔다(생존편향). 원인은 전략이 아니라 '거래대금 상위' 유니버스
자체가 −49% 침몰하는 투기 풀이었다는 것 — **시가총액 유니버스(top400)로 바꾸자 PIT +155.7%(CAGR +10.9%)로
알파가 복원**됐다(결정 0005·0006, 현재 라이브 적용). 이 저장소는 그 교훈 위에 세워졌다:
1. 모든 전략은 PIT 패널(`pit_ohlcv`)로 재검증 후 채택
2. 화면의 모든 수치는 빌드 시 원데이터에서 재계산(하드코딩 금지)
3. 공시(DART) 기반 악재 필터로 꼬리위험(상폐·희석) 사전 차단

## 전략 개요
| 축 | 내용 |
|---|---|
| 유니버스 | KOSPI+KOSDAQ **시가총액 상위 400**(20일 평균 거래대금 5억+·종가 1,000원+·상장 252거래일+, 우선주·스팩 제외) |
| 진입 | **F-리더(멀티팩터 주도주) ∩ 20주선 눌림** — 주도주가 추세선까지 눌릴 때만 매수(추격매수 회피) |
| 청산 | **40거래일 시간청산** 또는 **20주선 하향 이탈**(둘 중 먼저) |
| 노출 | **D4 레짐 신호**(40주선 breadth + 변동성 분위)로 목표 총노출 산정 → 슬롯·비중 결정 |
| 리스크 | **월 손실 −3% 서킷브레이커**(원금 대비 당월 손익, 도달 시 당월 신규매수 중단) |
| 보조 신호 | **TDA(위상수학)** = 변동성/난류 신호로 *자문 전용*(매도 트리거 아님) · **DART** 공시 악재 필터 |

전략 파라미터의 단일 진실원은 [`config/strategy.yaml`](config/strategy.yaml).

## 운영 화면 (웹 대시보드)
정보 밀도를 다섯 개 탭으로 분리한 단일 페이지 앱(`app/render/templates/trade_app.html`).
다크 모노크롬 테마, 보유·자본·매매일지는 브라우저 localStorage에 저장되고 구글 로그인 시 본인 서버에 동기화된다.

- **내 포트폴리오** — 보유 종목 카드(평가손익·목표/손절 참고가·TDA 위상리스크), 종목 클릭 시 매수 팝업·보유 클릭 시 매도 팝업(부분매도·실시간 손익 미리보기), KRX 호가단위(틱) 기반 가격 입력, 현금 투자액(원금)·서킷브레이커 상태.
- **종목 발굴** — 모멘텀·추세(F-리더 ∩ 20주선 눌림) 매수 후보와 위상수학(TDA) 안정+추세 후보를 2열로 비교. 카드 클릭으로 즉시 매수 입력.
- **시뮬레이터** — 알고리즘 **페이퍼 트레이딩**: 투자금·공격성(노출 ×1.0~2.5)·월 손실한도(−3/5/8%/끔)를 설정하면 서버 배치가 매일 전진. 구글 계정별로 서버에 저장되고, 토큰 만료·로그아웃 시에도 기기에 캐시된 마지막 상태를 *읽기 전용*(현재 값 아님 명시)으로 보여 사라지지 않는다.
- **백테스트** — 임의 기간·공격성·서킷브레이커로 즉석 백테스트(체결 내역·손익·승률·MDD·에쿼티 곡선). 사전 계산된 아카이브에 시뮬 엔진을 재생.
- **전략·검증** — 발굴 로직·TDA 기법·PIT 검증 방법·설계 근거를 사실 위주로 표기.

## 구조
```
app/
  data/       krx_api(공식 API) · fetchers(수집) · krx_loader · calendar · validators
  indicators/ daily · weekly · regime(D4) · leader(F리더) · pullback(20주선) · tda(위상수학)
  portfolio/  sizing · ledger · orders
  dart/       client(OpenDART) · filter(악재 분류: crit=상폐위험 / warn=희석·수급)
  render/     templates/trade_app.html(대시보드) · sectors
  sim/        engine(하루치 전진) · db(SQLite, 사용자별 상태) · backtest_cli(아카이브 재생)
  batch/      update_data(증분) · build_broad(전종목) · build_pit(시점별 패널)
              build_webapp(화면 생성) · run_sims(시뮬 전진) · build_bt_archive(백테스트 아카이브)
server/       sync_api(구글 ID토큰 검증 → 사용자별 동기화·시뮬 API) · leaders-sync.service(systemd)
backtest/     portfolio · pit_portfolio(생존편향 제거) · circuit_breaker · overnight · overlap · tda_exit
config/       strategy.yaml (전략 파라미터 — 단일 진실원)
docs/         architecture · validation-status · quality-gates · runbooks · decisions/
scripts/      server_daily_batch.sh(매일 08:10 cron) · deploy
state/        실행 산출물(빌드 결과·시뮬 DB·아카이브) — git 미추적
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
구글 동기화·시뮬레이터를 로컬에서 쓰려면 `GOOGLE_CLIENT_ID`를 설정하고 `server/sync_api.py`를 함께 띄운다
(미설정 시 화면은 localStorage 단독 모드로 동작).

## 전략 검증 현황 (정직 고지)
| 전략 | 생존 패널 | **PIT(신뢰 기준)** | 상태 |
|---|---|---|---|
| 모멘텀·추세(F리더∩20주선 눌림, **시총 top400**) | +531% | **+155.7%** (CAGR +10.9%, MDD −39%, 풀 −2.5% 대비 +158p, 양 반기 +) | ✅ 라이브 적용(시총 top400, 결정 0005·0006) |
| └ 시총 top300(이전) | +531% | +67.7% (전반 −8%=비견고) | 집중도검증서 top400로 상향(결정 0006) |
| └ 이전 거래대금 유니버스 | +531% | −20.4% (풀 −49% 투기 드리프트) | ❌ 폐기(생존편향이 가렸던 풀 침몰) |
| 복합(장기80%+단타20%) + 월손실 −3% 서킷브레이커 | — | **CAGR +8.3%, MDD −25%, 최악월 −6.3%** | ✅ 운영 구성(결정 0004) |
| TDA 매수 | 알파 없음 | — | 매도/리스크 신호로만 사용(자문 전용) |
| 오버나이트 1박(거래량 폭발) | CAGR +1.3% | Risk-Off만 net+ | 보조(≤20% 분산용, 단독 엣지≈0) |
| DART 악재 필터 | — | — | 분류기 검증 통과, 효과 백테스트 예정 |

> **벤치마크 정직 고지**: 같은 기간 **KOSPI(시총가중) 매수 후 보유는 +257%(CAGR 15.1%)** 로 이 전략(+155.7%)을
> **상회**한다 — 지수는 소수 초대형주가 견인. '동일가중 풀 대비 선별 알파'는 실재하지만 '인덱스 초과수익'은
> 아니다(이 강세장 한정). 이 전략의 가치는 **더 낮은 MDD와 국면 분산**에 있으며, 레짐 의존(2018·2019·2022·2024 음)으로
> 깊은 낙폭을 서킷브레이커로 관리한다. **개별 종목의 수익은 보장하지 않는다.**

상세: [`docs/validation-status.md`](docs/validation-status.md) · 검증 기준: [`docs/quality-gates.md`](docs/quality-gates.md)

## 일일 운영
서버 cron `10 8 * * 1-6`(월~토 08:10, 공식 API가 전일 EOD를 매일 08시 갱신) → `scripts/server_daily_batch.sh`:
증분 데이터 → 전종목 패널 → 화면 생성(`/var/www/leaders/index.html`) → 시뮬 전진 → 백테스트 아카이브.
운영 절차·장애 복구는 [`docs/runbook-daily-ops.md`](docs/runbook-daily-ops.md), [`docs/runbook-recovery.md`](docs/runbook-recovery.md).

## 동기화·보안
- 인증: 구글 로그인으로 받은 **ID 토큰을 서버에서 로컬 검증**(google-auth, audience=`GOOGLE_CLIENT_ID`)해 사용자 `sub` 추출. 클라이언트 시크릿은 사용·저장하지 않는다.
- 저장: 보유·일지는 `state/userdata/{sub}.json`, 시뮬은 `state/sim.db`(SQLite) — 본인 `sub`로만 접근(교차 접근 불가). 자체 호스팅, 외부 전송 없음.
- 자격증명: `.env`는 gitignore 대상이며 커밋하지 않는다.

## 에이전트로 작업하기
Claude Code 등 코딩 에이전트는 **[`AGENTS.md`](AGENTS.md)** 를 먼저 읽는다 — 검증 규칙·금지 사항·
정의된 완료 조건이 거기에 있다. 사람 운영자는 이 README와 `docs/runbook-*`를 따른다.
구조적 결정은 `docs/decisions/`에 기록한다.
