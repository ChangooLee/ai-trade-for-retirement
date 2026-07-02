"""글로벌 ETF 로테이션 신호 — 코어 자산 선택(전부 KRX 상장 KRW ETF, 토스 집행 가능).

검증(backtest/global_rotation_backtest.py, 2016~2026): KR2+나스닥100 주간 로테이션
CAGR +26.0%·MDD −25%·Sharpe 1.15 — KR-only(+17.9%/−43%) 압도. 금(132030)은 수익을 깎아 미포함.
규칙: 200일선 위 자산 중 65일(13주) 모멘텀 최상위 1개 보유, 전부 추세 아래면 현금.
데이터: 토스 캔들(소유자 키) 실시간 — 별도 배치 불필요.
"""
from __future__ import annotations

ASSETS = {"069500": "KODEX 200", "229200": "KODEX 코스닥150", "133690": "TIGER 나스닥100"}


def signal(api_key, secret):
    """{assets:{sym:{name,close,ma200,above,mom13w}}, target, target_name} — target=None이면 현금."""
    from app.data import toss_api as T
    out = {"assets": {}, "target": None, "target_name": None}
    best = None
    for sym, name in ASSETS.items():
        closes, before = [], None
        for _ in range(2):                       # 400일(200MA+13주 모멘텀 충분)
            c = T.get_candles(api_key, secret, sym, interval="1d", count=200, before=before)
            cs = (c.get("result") or {}).get("candles") or []
            closes.extend(float(x["closePrice"]) for x in cs)
            if len(cs) < 200:
                break
            before = cs[-1]["timestamp"]
        closes.reverse()                          # 과거→현재
        if len(closes) < 210:
            continue
        cur = closes[-1]
        ma200 = sum(closes[-200:]) / 200.0
        mom = (cur / closes[-66] - 1) if len(closes) >= 66 else 0.0
        above = cur > ma200
        out["assets"][sym] = {"name": name, "close": cur, "ma200": round(ma200, 1),
                              "above": above, "mom13w": round(mom, 4)}
        if above and (best is None or mom > best[1]):
            best = (sym, mom)
    if best:
        out["target"] = best[0]
        out["target_name"] = ASSETS[best[0]]
    return out
