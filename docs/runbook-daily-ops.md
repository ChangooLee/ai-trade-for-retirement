# 런북 — 일일 운영

## 자동 (서버 cron)
- `10 8 * * 1-6` → `scripts/server_daily_batch.sh` (월~토 08:10 KST, **아침=전일 EOD로 유니버스 재선정+추천**). 로그: `~/ai-trade-for-retirement/logs/batch_YYYYMMDD.log`
- `0 17 * * 1-5` → `scripts/server_intraday_batch.sh` (월~금 17:00 KST, **오후=당일 종가 same-day 갱신**). 로그: `logs/intraday_YYYYMMDD.log`
  - 공식 KRX OpenAPI는 당일치를 **익일 08시**에야 공개 → 오후엔 유니버스 재선정 불가. 하지만 **pykrx는 장후 당일 종가 제공** → 시뮬·백테·페이지 가격을 '오늘 종가'로 갱신.
  - 단계: 1) `build_intraday`(pykrx 당일종가 기존 유니버스에 append) → 2) `build_webapp --asof=<daily_ohlcv 최신일>`(★`--asof` 필수: 미지정 시 기본=공식 최신=전일이라 same-day 무효★) → 3) `run_sims`(시뮬 당일 전진) → 4) `build_bt_archive`(백테 당일 추가) → 5) `build_flow`(당일 수급).
  - 추천(주봉 20주선 눌림)은 완성 주봉 기반이라 장중 안 바뀜 → 아침 추천 유지, 오후엔 가격·시뮬만 same-day.
- 단계(set -uo pipefail — 한 단계 실패해도 다음 진행, rc는 마지막값):
  1) update_data(상위 유니버스 증분·pykrx 수정주가 + 인덱스 공식API)
  2) build_broad(전종목 공식API)
  3) build_webapp → `/var/www/leaders/index.html` (+ `state/daily_signals.json`, `state/reco_history.json`)
  4) run_sims(로그인 사용자별 페이퍼 시뮬 전진)
  5) build_bt_archive(`state/bt_days.json`·`bt_prices.parquet` — 추천 백필·기간백테스트용)
  6) build_overnight_archive
  7) build_minute(KIS 1분봉 증분 — 1년보관, 매일 축적 필수)
  8) build_flow(KIS 외인/기관/개인 수급 증분 — 30일 트레일링)
- KRX EOD 08:00 공개 → **08:17경 화면 갱신**.

## 서비스 (systemd)
- `leaders-sync` — sync API(구글 동기화 `/api/sync`, KIS 시세 `/api/quote`, 틱 SSE `/api/stream`, DART `/api/dart`). 127.0.0.1:8799, nginx `/trading/api/` 프록시.
- 상태: `systemctl is-active leaders-sync` · 재시작: `sudo systemctl restart leaders-sync`(sync_api.py 변경 시 필수).

## 코드 배포 (SFTP — 서버는 rsync 모델, git pull 아님)
```bash
# 1) 변경 파일 SFTP 업로드 (예: app/dart/filter.py)
sshpass -f <pwfile> sftp lchangoo@125.240.175.68  # cd ai-trade-for-retirement; put <path> <path>
# 2) sync_api.py 변경 시: sudo systemctl restart leaders-sync
# 3) 템플릿/배치 변경 시: 웹앱 재빌드(아래 수동 빌드)
# 4) nginx 변경 시: sudo nginx -t && sudo systemctl reload nginx (백업: /etc/nginx/sites-enabled/*.bak.*)
```

## ★알려진 이슈 (2026-06-30 점검)★
- **KRX_ID/KRX_PW 미설정** → 공식 API 로그인 실패 → **index_ohlcv 고정(현재 06-26)·build_broad 미갱신**. daily_ohlcv는 pykrx라 정상(현재). D4 국면은 40주MA 기반이라 1~3일 stale 영향 미미하나 누적 방치 금지. 해결: 서버 `.env`에 KRX_ID/KRX_PW 설정 또는 인덱스 pykrx 폴백 추가.
- (수정됨) build_bt_archive `mktcap` KeyError — 서버 daily_ohlcv에 mktcap 없어 avg_trdval20 폴백.

## 운영자 데일리 루틴 (화면의 '데일리 루틴' 문구와 동일)
1. 08:17+ 화면 확인 (전일 신호·국면)
2. 08:30–09:00 오버나이트 보유분 시가 매도 주문(전날 예약주문 가능)
3. 15:20 워치리스트 HTS 재확인(거래량×3·등락+3~28%·상한가 미잠김) → 충족 종목만 종가 매수
4. DART 배지(🚫 crit) 종목은 신호와 무관하게 매수 금지

## 수동 빌드 (서버)
```bash
ssh lchangoo@125.240.175.68
cd ~/ai-trade-for-retirement && .venv/bin/python -m app.batch.build_webapp --out /var/www/leaders/index.html
```
