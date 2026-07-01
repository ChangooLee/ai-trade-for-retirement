"""보유기간(시간청산 H) 스윕 — 아카이브(bt_days/bt_prices) 포트폴리오 리플레이.
엔진 로직 동일: 청산=보유 H거래일 OR 추세이탈(sells), D4 노출 m 사이징, 종가집행, 왕복비용.
★주의: 서버 bt_prices는 현 top400 daily_ohlcv 기반이라 생존편향 있음 — 절대수치는 부풀려도
40/60/80 상대비교는 유효(편향이 H에 대체로 균일). 생존편향 제거는 PIT 패널로 별도 확인.★
사용: python -m backtest.hold_sweep_archive [--from 2017-05-01] [--holds 40,60,80,100,120]
"""
from __future__ import annotations
import argparse, json, math, os, sys
import numpy as np, pandas as pd, yaml
sys.path.insert(0, ".")
from app.portfolio.sizing import compute_target_slots, compute_weight_per_stock  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cal, days, px_by_day, H, cap0, maxpos, baseslot, cost, start_i):
    cash = float(cap0); pos = {}; eqc = []; trades = []
    for i in range(start_i, len(cal)):
        d = cal[i]; pxd = px_by_day.get(d, {})
        eqc.append((d, cash + sum(p["sh"] * pxd.get(tk, p["epx"]) for tk, p in pos.items())))
        day = days[d]; sells = set(day.get("sells") or []); m = float(day.get("m", 0.4))
        for tk, p in list(pos.items()):                     # 청산: 시간 H OR 추세이탈
            cpx = pxd.get(tk)
            if cpx is None:
                continue
            if (i - p["eidx"]) >= H or tk in sells:
                cash += p["sh"] * cpx * (1 - cost / 2); trades.append(cpx / p["epx"] - 1); del pos[tk]
        slots = compute_target_slots(m, maxpos, baseslot)
        weight = compute_weight_per_stock(m, slots)
        if slots > len(pos):                                 # 매수: 빈 슬롯만큼 당주차 후보
            eq_now = cash + sum(p["sh"] * pxd.get(tk, p["epx"]) for tk, p in pos.items())
            for tk in (day.get("buy") or []):
                if len(pos) >= slots:
                    break
                if tk in pos:
                    continue
                bpx = pxd.get(tk)
                if not bpx or bpx <= 0:
                    continue
                sh = math.floor(weight * eq_now / bpx); amt = sh * bpx * (1 + cost / 2)
                if sh > 0 and cash >= amt:
                    cash -= amt; pos[tk] = {"eidx": i, "epx": bpx, "sh": sh}
    pxl = px_by_day.get(cal[-1], {})
    final = cash + sum(p["sh"] * pxl.get(tk, p["epx"]) for tk, p in pos.items())
    eq = pd.DataFrame(eqc, columns=["d", "eq"]); eq["d"] = pd.to_datetime(eq["d"]); eq = eq.set_index("d")
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (final / cap0) ** (1 / yrs) - 1 if final > 0 else -1
    dd = (eq["eq"] / eq["eq"].cummax() - 1).min()
    r = eq["eq"].pct_change().dropna()
    shp = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    winr = float(np.mean([t > 0 for t in trades])) if trades else 0
    return dict(H=H, final=final, ret=final / cap0 - 1, cagr=cagr, mdd=dd, sharpe=shp,
                n=len(trades), winr=winr, avg=float(np.mean(trades)) if trades else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="2017-05-01")
    ap.add_argument("--holds", default="40,60,80,100,120")
    args = ap.parse_args()
    cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
    maxpos = cfg["sizing"]["max_positions"]; baseslot = cfg["sizing"]["base_slot_weight"]
    cost = cfg["cost"]["assumed_round_trip_cost"]; cap0 = cfg["portfolio"]["initial_capital"]
    a = json.load(open(os.path.join(_REPO, "state", "bt_days.json"), encoding="utf-8"))
    days = a["days"]
    btp = pd.read_parquet(os.path.join(_REPO, "state", "bt_prices.parquet"))
    btp["date"] = btp["date"].astype(str).str[:10]
    px_by_day = {d: dict(zip(g["ticker"], g["close"].astype(float))) for d, g in btp.groupby("date")}
    cal = sorted(days.keys())
    start_i = next((i for i, d in enumerate(cal) if d >= args.frm), 0)
    print(f"아카이브 리플레이 {cal[start_i]}~{cal[-1]} ({len(cal)-start_i}거래일) · 보유기간 스윕 {args.holds}")
    print("  ※ 생존편향 있는 아카이브(top400) — 상대비교용. 절대수치는 PIT로 별도 확인.")
    for H in [int(x) for x in args.holds.split(",")]:
        m = run(cal, days, px_by_day, H, cap0, maxpos, baseslot, cost, start_i)
        print(f"  H={H:3d}거래일: CAGR {m['cagr']:+.1%} | MDD {m['mdd']:+.1%} | Sharpe {m['sharpe']:.2f} | "
              f"거래 {m['n']} 승률 {m['winr']:.0%} 평균 {m['avg']:+.2%} | 총 {m['ret']:+.0%}")


if __name__ == "__main__":
    main()
