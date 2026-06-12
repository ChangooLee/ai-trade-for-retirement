# 런북 — 장애 복구

## 화면이 안 갱신될 때
1. `tail ~/ai-trade-for-retirement/logs/batch_$(date +%Y%m%d).log` — 실패 단계 확인
2. KRX EOD 미발표(휴장/지연)면 정상 — asof가 직전 거래일로 유지됨
3. 부분 실패 시 해당 배치만 재실행 (update_data → build_broad → build_webapp 순서 유지)

## 데이터 캐시 손상/스키마 변경
- 상위 유니버스: `rm data/cache/daily_ohlcv.parquet` 후 update_data (전체 재수집 ~6분)
- 전종목: build_broad는 날짜 단위 재개 가능 (이미 수집한 날짜 스킵)
- PIT: build_pit 재실행 (보유 날짜 스킵, 전체 ~2시간)

## nginx / 사이트
- 백업: /tmp/sp.bak* . 수정 후 `sudo nginx -t && sudo systemctl reload nginx`
- /trading 500 오류: alias 대상 파일 존재·권한(lchangoo) 확인

## 롤백
- 이전 프로젝트(~/leaders)가 동결 보존됨 — cron 경로만 되돌리면 즉시 롤백
