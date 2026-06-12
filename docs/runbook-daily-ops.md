# 런북 — 일일 운영

## 자동 (서버 cron)
- `10 8 * * 1-6` → `scripts/server_daily_batch.sh`
  1) update_data(상위 유니버스 증분) 2) build_broad(전종목 +1일) 3) build_webapp → /var/www/leaders/index.html
- KRX가 전일 EOD를 08:00 공개 → **08:17경 화면 갱신**. 로그: `~/ai-trade-for-retirement/logs/batch_YYYYMMDD.log`

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
