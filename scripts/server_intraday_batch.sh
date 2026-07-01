#!/bin/bash
# LEADERS DESK 서버 '오후(장후)' 배치 — 당일 종가 same-day 갱신.
# 아침 배치(08:10)는 전일 EOD(공식 API)로 유니버스 재선정+추천. 공식 API는 당일치를 익일 08시에야 공개.
# ★하지만 pykrx는 장후 당일 종가 제공 → 시뮬·백테스트·페이지 가격을 '오늘 종가'로 same-day 갱신★
# (추천은 완성 주봉 기반이라 장중 안 바뀜 → 아침 추천 유지. 오후엔 유니버스 재선정/build_broad 안 함.)
# cron(월~금 17:00 KST):  0 17 * * 1-5  /home/lchangoo/ai-trade-for-retirement/scripts/server_intraday_batch.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
# pykrx 회원 로그인 자격(.env의 KRX_ID/KRX_PW) os.environ 노출 — pykrx가 os.getenv로 읽음.
set -a; . <(grep -E "^KRX_(ID|PW)=" "$ROOT/.env" 2>/dev/null) || true; set +a
PUB="/var/www/leaders/index.html"
mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/intraday_$(date +%Y%m%d).log"
{
  echo "==== $(date '+%F %T') 오후 배치 시작 ===="
  echo "-- 1) 당일 종가 적재(pykrx, 기존 유니버스에 오늘 1행 append)"
  "$PY" -m app.batch.build_intraday
  # asof = daily_ohlcv 실제 최신일(build_intraday 성공 시 오늘, 휴장/미수집 시 전일). 공식 최신(전일)로 빌드되는 것 방지.
  ASOF="$("$PY" -c "import yaml;from app.data import krx_loader as L;c=yaml.safe_load(open('config/strategy.yaml',encoding='utf-8'));print(str(L.load_daily_ohlcv(c['paths']['daily_ohlcv'])['date'].max().date()).replace('-',''))" 2>/dev/null)"
  echo "-- 2) 웹앱 재빌드(--asof=$ASOF, 당일 종가 반영) → $PUB"
  "$PY" -m app.batch.build_webapp --asof "$ASOF" --out "$PUB"
  rc=$?
  echo "-- 3) 활성 시뮬레이션 당일 전진(오늘 종가 마킹)"
  "$PY" -m app.batch.run_sims
  echo "-- 4) 기간 백테스트 아카이브에 오늘 추가"
  "$PY" -m app.batch.build_bt_archive
  echo "-- 5) KIS 수급 증분(당일 외인/기관/개인) — 30일 트레일링"
  "$PY" -m app.batch.build_flow --top 400 || echo "  (수급 적재 실패 — 무시하고 계속)"
  echo "==== $(date '+%F %T') 오후 배치 종료 (rc=$rc) ===="
} >> "$LOG" 2>&1
find "$ROOT/logs" -name 'intraday_*.log' -mtime +30 -delete 2>/dev/null
