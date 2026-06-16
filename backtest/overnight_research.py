"""오버나이트 단타 연구 하버스 — 신호 규칙을 바꿔가며 2026 + 전체기간(2017~2025) 동시 평가.

목적: 2026 단타 수익 극대화 방향 연구. ★과최적화 방지: 모든 설정을 2026 '그리고' 2017~2025에서 평가해
'2026에서만 좋은가(curve-fit) vs 늘 좋은가(일반화)'를 한눈에 비교.
데이터: daily_ohlcv(화면 단타와 동일 유동성 유니버스). 가정: 종가 매수 → 익일 시가 매도, 왕복 0.35%/박.

설정 인자: --vol-mult(거래량 배수) --ret-lo --ret-hi(등락 밴드) --topk --weight(equal|trdval)
           --tv-min(최소 20일평균 거래대금) --gate(none|skip_off|only_off)
출력: 2026 / 2017~2025 각각 노출30·50·100% 누적·MDD + 밤 평균/승률/분위 + 2026 월별·최고최저 밤. JSON+요약.
사용: .venv/bin/python -m backtest.overnight_research --vol-mult 3 --ret-lo 0.03 --ret-hi 0.285 --topk 8
"""
from __future__ import annotations
import argparse, json, sys
import numpy as np, pandas as pd, yaml
sys.path.insert(0, ".")
from app.data import krx_loader as L  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402

COST = 0.0035
_CACHE = {}


def _prep():
    if _CACHE:
        return _CACHE
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"]); index = L.load_index_ohlcv(cfg["paths"]["index_ohlcv"])
    P = {c: daily.pivot_table(index="date", columns="ticker", values=c) for c in ("open", "close", "volume", "trdval")}
    for c in ("open", "close"):
        P[c] = P[c].where(P[c] > 0)
    o, c_, v, tv = P["open"], P["close"], P["volume"], P["trdval"]
    feats = {"ret1": c_.pct_change(), "vol20": v.rolling(20).mean(), "tv20": tv.rolling(20).mean(),
             "r_on": o.shift(-1) / c_ - 1, "v": v, "tv": tv}
    dates = c_.index
    # D4 국면(주 단위 캐시)
    wkk = pd.Series(dates, index=dates).dt.to_period("W").astype(str); mc = {}; modes = []
    for d, w in zip(dates, wkk):
        if w not in mc:
            try: mc[w] = compute_d4_exposure(index[index["date"] <= d], d, cfg)["mode"]
            except Exception: mc[w] = "?"
        modes.append(mc[w])
    feats["mode"] = pd.Series(modes, index=dates)
    _CACHE.update(feats); _CACHE["dates"] = dates
    return _CACHE


def nightly_returns(vol_mult, ret_lo, ret_hi, topk, weight, tv_min):
    f = _prep()
    sig = (f["v"] > vol_mult * f["vol20"]) & (f["ret1"] > ret_lo) & (f["ret1"] < ret_hi) & (f["tv20"] > tv_min)
    picked = f["tv"].where(sig).rank(axis=1, ascending=False) <= topk
    ron = f["r_on"].where(picked)
    if weight == "trdval":
        w = f["tv"].where(picked); night = (ron * w).sum(axis=1) / w.sum(axis=1)
    else:
        night = ron.mean(axis=1)
    return night, picked.sum(axis=1)


def stats(night, npicks, modes, lo, hi, gate):
    idx = (night.index >= lo) & (night.index <= hi)
    n = night[idx].copy(); nm = modes[idx]
    if gate == "skip_off":
        n = n.where(nm != "Risk-Off")
    elif gate == "only_off":
        n = n.where(nm == "Risk-Off")
    traded = n.dropna()
    out = {"trade_nights": int((npicks[idx] > 0).sum() if gate == "none" else traded.notna().sum())}
    out["trade_nights"] = int(traded.shape[0])
    if traded.empty:
        return {"trade_nights": 0, "ret30": 0, "ret50": 0, "ret100": 0, "mdd30": 0, "mdd100": 0,
                "night_mean": 0, "night_med": 0, "win": 0, "p05": 0, "p95": 0}
    net = traded - COST                               # 밤당 net(비용 차감, 100% 가정)
    def eq_stats(exp):
        eqs = (1 + exp * net).cumprod()
        peak = eqs.cummax(); mdd = float((eqs / peak - 1).min())
        return float(eqs.iloc[-1] - 1), mdd
    r30, m30 = eq_stats(0.3); r50, m50 = eq_stats(0.5); r100, m100 = eq_stats(1.0)
    return {"trade_nights": int(traded.shape[0]), "ret30": r30, "ret50": r50, "ret100": r100,
            "mdd30": m30, "mdd100": m100, "night_mean": float(traded.mean()), "night_med": float(traded.median()),
            "net_mean": float(net.mean()), "win": float((net > 0).mean()),
            "p05": float(traded.quantile(0.05)), "p95": float(traded.quantile(0.95))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol-mult", type=float, default=3.0); ap.add_argument("--ret-lo", type=float, default=0.03)
    ap.add_argument("--ret-hi", type=float, default=0.285); ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--weight", default="equal", choices=["equal", "trdval"])
    ap.add_argument("--tv-min", type=float, default=1e9); ap.add_argument("--gate", default="none", choices=["none", "skip_off", "only_off"])
    a = ap.parse_args()
    f = _prep()
    night, npicks = nightly_returns(a.vol_mult, a.ret_lo, a.ret_hi, a.topk, a.weight, a.tv_min)
    modes = f["mode"]
    s26 = stats(night, npicks, modes, pd.Timestamp("2026-01-01"), pd.Timestamp("2026-12-31"), a.gate)
    sIS = stats(night, npicks, modes, pd.Timestamp("2017-01-01"), pd.Timestamp("2025-12-31"), a.gate)
    # 2026 월별 + 최고/최저 밤
    n26 = night[(night.index >= "2026-01-01")].dropna() - COST
    monthly = {str(p): round(float((1 + g).prod() - 1), 4) for p, g in n26.groupby(n26.index.to_period("M"))}
    top = [(str(d.date()), round(float(v), 4)) for d, v in (night[night.index >= "2026-01-01"] - COST).dropna().sort_values(ascending=False).head(5).items()]
    bot = [(str(d.date()), round(float(v), 4)) for d, v in (night[night.index >= "2026-01-01"] - COST).dropna().sort_values().head(5).items()]
    cfg = {"vol_mult": a.vol_mult, "ret_lo": a.ret_lo, "ret_hi": a.ret_hi, "topk": a.topk, "weight": a.weight, "tv_min": a.tv_min, "gate": a.gate}
    res = {"config": cfg, "y2026": s26, "y2017_2025": sIS, "monthly_2026": monthly, "top_nights_2026": top, "bot_nights_2026": bot}
    print(json.dumps(res, ensure_ascii=False))
    print(f"\n[설정] {cfg}", file=sys.stderr)
    print(f"[2026]     노출30% {s26['ret30']:+.1%}(MDD{s26['mdd30']:+.0%}) · 100% {s26['ret100']:+.1%}(MDD{s26['mdd100']:+.0%}) · "
          f"밤 {s26['trade_nights']}·net평균 {s26.get('net_mean',0):+.2%}·승률 {s26['win']:.0%}·p05 {s26['p05']:+.1%}", file=sys.stderr)
    print(f"[17~25]    노출30% {sIS['ret30']:+.1%}(MDD{sIS['mdd30']:+.0%}) · 100% {sIS['ret100']:+.1%}(MDD{sIS['mdd100']:+.0%}) · "
          f"밤 {sIS['trade_nights']}·net평균 {sIS.get('net_mean',0):+.2%}·승률 {sIS['win']:.0%}", file=sys.stderr)


if __name__ == "__main__":
    main()
