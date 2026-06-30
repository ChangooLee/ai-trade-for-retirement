#!/bin/bash
# LEADERS DESK 서버 일일 배치
# 전일 EOD(공식 API가 매일 08시 갱신)로 증분 업데이트 → 웹앱 재빌드 → /var/www/leaders/index.html 게시.
# cron(월~금 08:10):  10 8 * * 1-5  /home/lchangoo/ai-trade-for-retirement/scripts/server_daily_batch.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
# pykrx 회원 로그인 자격(.env의 KRX_ID/KRX_PW)을 os.environ에 노출 — pykrx auth.py가 os.getenv로 읽음.
# (KRX가 데이터 엔드포인트를 점점 로그인 게이팅 → 안정적 수집 위해. 비밀은 .env에서만, 명령줄 노출 없음.)
set -a; . <(grep -E "^KRX_(ID|PW)=" "$ROOT/.env" 2>/dev/null) || true; set +a
PUB="/var/www/leaders/index.html"
mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/batch_$(date +%Y%m%d).log"
{
  echo "==== $(date '+%F %T') 배치 시작 ===="
  echo "-- 1) 증분 데이터 업데이트(상위 유니버스, pykrx 수정주가)"
  "$PY" -m app.batch.update_data
  echo "-- 2) 전 종목 브로드 갱신(공식 API, 분석용)"
  "$PY" -m app.batch.build_broad
  echo "-- 3) 웹앱 재빌드 → $PUB (+ state/daily_signals.json 산출)"
  "$PY" -m app.batch.build_webapp --out "$PUB"
  rc=$?
  echo "-- 4) 활성 시뮬레이션 일별 전진(로그인 사용자별 페이퍼 매매)"
  "$PY" -m app.batch.run_sims
  echo "-- 5) 기간 백테스트 아카이브 증분 갱신(state/bt_days.json·bt_prices.parquet)"
  "$PY" -m app.batch.build_bt_archive
  echo "-- 6) 단타 백테스트 아카이브 증분 갱신(state/overnight_days.json)"
  "$PY" -m app.batch.build_overnight_archive
  echo "-- 7) KIS 분봉 증분 적재(최근 3일, 거래대금 상위44+보유) — 1년보관이라 매일 축적 필수"
  "$PY" -m app.batch.build_minute --days 3 --top 44 || echo "  (분봉 적재 실패 — 무시하고 계속)"
  echo "-- 8) KIS 수급 증분 적재(외인/기관/개인 순매수, 상위400) — 30일 트레일링이라 매일 축적 필수"
  "$PY" -m app.batch.build_flow --top 400 || echo "  (수급 적재 실패 — 무시하고 계속)"
  echo "==== $(date '+%F %T') 종료 (rc=$rc) ===="
} >> "$LOG" 2>&1
# 로그 30일 보관
find "$ROOT/logs" -name 'batch_*.log' -mtime +30 -delete 2>/dev/null
