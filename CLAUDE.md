# CLAUDE.md

**먼저 [AGENTS.md](AGENTS.md)를 읽어라** — 검증 규칙·금지사항·완료 조건의 단일 계약이다.
이 파일은 Claude Code 전용 실행 정보만 담는다.

## 자주 쓰는 명령
```bash
# 가상환경 (없으면: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
.venv/bin/python -m app.batch.update_data                      # 상위 유니버스 증분(pykrx 수정주가)
.venv/bin/python -m app.batch.build_broad                      # 전종목 패널(공식 API, 증분)
.venv/bin/python -m app.batch.build_pit                        # 시점별 패널(상폐 포함 — 백테스트용, 장시간)
.venv/bin/python -m app.batch.build_webapp --out state/trade.html   # 화면 생성(DART 주석 포함)
.venv/bin/pytest tests/ -q                                     # 테스트
.venv/bin/python -m backtest.pit_portfolio_backtest            # 생존편향 제거 백테스트(신뢰 기준)
```

## 환경
- `.env`(커밋 금지): `KRX_AUTH_KEY`, `OPENDART_API_KEY` — `.env.example` 참고
- 데이터 캐시: `data/cache/*.parquet` (생성물, 커밋 금지). 없으면 위 배치들이 생성
- 출력 노이즈: pandas FutureWarning·"KRX 로그인 실패"(pykrx 부가 경고, 무해)는 grep -v로 걸러 읽기

## 배포 (개발서버)
- 서버: `125.240.175.68` (lchangoo), 프로젝트 `~/ai-trade-for-retirement`, 출력 `/var/www/leaders/index.html`
- cron `10 8 * * 1-6` → `scripts/server_daily_batch.sh` (KRX가 전일 EOD를 08:00 공개 → 08:17경 화면 갱신)
- **코드 수정 후 서버 sftp 동기화 필수** — 안 하면 다음날 cron이 구버전으로 빌드
- nginx: sourceport.ai `/trading` → 위 출력 파일 alias (no-cache, gzip). nginx 수정 시 백업+`nginx -t`

## 함정 (실제로 겪은 것)
- 생존 패널(daily_ohlcv)로 전략 성과를 주장하지 말 것 — PIT 패널이 신뢰 기준 (AGENTS.md §2)
- pykrx 캐시는 (from,to) 키 — asof 바뀌면 전체 재다운로드라 증분은 update_data 경유
- 주봉 신호는 완성주만(calendar.last_completed_week_cutoff) — 주중 부분봉으로 신호 흔들림 방지
- 거래정지일 가격 0 → 수익률 inf 오염: 가격 피벗엔 `.where(>0)` 필수
- CSV 왕복 시 ticker가 int로 변함 → `astype(str).str.zfill(6)`
- 화면 통계는 빌드 시 재계산(상수 금지) — overnight stats·국면분해가 그 예
