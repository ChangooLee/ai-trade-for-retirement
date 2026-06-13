#!/bin/bash
# LEADERS DESK 서버 일일 배치
# 전일 EOD(공식 API가 매일 08시 갱신)로 증분 업데이트 → 웹앱 재빌드 → /var/www/leaders/index.html 게시.
# cron(월~금 08:10):  10 8 * * 1-5  /home/lchangoo/ai-trade-for-retirement/scripts/server_daily_batch.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
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
  echo "==== $(date '+%F %T') 종료 (rc=$rc) ===="
} >> "$LOG" 2>&1
# 로그 30일 보관
find "$ROOT/logs" -name 'batch_*.log' -mtime +30 -delete 2>/dev/null
