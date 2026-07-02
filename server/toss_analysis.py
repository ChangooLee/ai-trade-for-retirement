"""보유종목 우리전략 분석 — 전 KRX 종목의 F리더·RS·52주고점·20주선 신호(30분 캐시).

유니버스(상위394) 밖 보유종목도 신호를 주기 위해 daily_ohlcv 전 종목에 지표를 계산.
무겁(수 초)지만 모듈 캐시로 최초 1회만. verdict는 클라이언트(tossSignal)가 파생.
"""
from __future__ import annotations
import threading, time

_cache = {"ts": 0.0, "sig": {}}
_lock = threading.Lock()
_TTL = 1800.0


def _compute():
    import os
    import pandas as pd, yaml
    _REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = yaml.safe_load(open(os.path.join(_REPO, "config", "strategy.yaml"), encoding="utf-8"))
    from app.data import krx_loader as L
    from app.indicators.daily import add_daily_indicators
    from app.indicators.weekly import to_weekly, add_weekly_indicators
    from app.indicators.leader import compute_leader_flags
    daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
    asof = daily["date"].max()
    daily = daily[daily["date"] <= asof]
    di = add_daily_indicators(daily)
    da = di.sort_values(["ticker", "date"]).groupby("ticker").tail(1).copy()   # 전 종목(유니버스 제한 없음)
    leaders = compute_leader_flags(da, cfg)                                    # RS는 전 종목 대비 순위
    wk = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    wa = wk.sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)[["ticker", "w_ma20", "dist_wma20", "pullback_20w_105"]]
    m = leaders.merge(wa, on="ticker", how="left")

    def _n(v, d=0.0):
        try:
            v = float(v)
            return v if v == v else d      # NaN 방어
        except (TypeError, ValueError):
            return d

    sig = {}
    for _, r in m.iterrows():
        wma = r.get("w_ma20")
        sig[str(r["ticker"])] = {
            "leader": bool(r.get("is_f_leader")) if pd.notna(r.get("is_f_leader")) else False,
            "rs": round(_n(r.get("rs_rank"))),
            "high52": round(_n(r.get("high_52w_ratio")), 3),
            "close": _n(r.get("close")),
            "wma20": (round(_n(wma)) if pd.notna(wma) else None),
        }
    return sig


def signals(symbols):
    """요청 심볼들의 신호 dict {sym: {leader,rs,high52,close,wma20}} — 없으면 None. 30분 캐시."""
    now = time.time()
    with _lock:
        if now - _cache["ts"] > _TTL or not _cache["sig"]:
            try:
                _cache["sig"] = _compute()
                _cache["ts"] = now
            except Exception:
                pass
        s = _cache["sig"]
    return {str(x): s.get(str(x)) for x in symbols}
