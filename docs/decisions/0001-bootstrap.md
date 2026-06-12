# 0001 — 프로젝트 부트스트랩 (2026-06-12)
mcp-krx에서 트레이딩 시스템을 분리해 ai-trade-for-retirement로 이관.
- 의존성 내재화: krx_openapi_client→app/data/krx_api, 수집기→app/data/fetchers, 섹터맵→app/render/sectors
- 레거시 화면(run_daily/daily_html/svg_candles) 미이관 — 웹앱 파이프라인만 유지, BACKTEST 상수는 backtest_stats로 분리
- master-swarm 프롬프트에서 채택: AGENTS.md 기계계약·instruction precedence·quality gates·runbooks·decisions 로그·bounded retry.
  미채택: 범용 스킬/에이전트 카탈로그(18종 등) — 단일 도메인 프로젝트에 과잉, 필요 시 추가
- DART는 mcp-opendart 서버 없이 REST API 직접 사용(사용자 결정)
