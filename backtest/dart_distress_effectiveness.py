"""검증 게이트 — 재무 부실(자본잠식·부채과다·유동성부족) 종목 회피가 손실을 줄이나?
터미널 공시 필터(공시 後 작동)를 상류로 확장: 신호일 시점 '최근 정기보고서'의 재무로 부실을 조기 판정.
★룩어헤드 차단: 신호일 이전 접수(rcept_dt<=signal)된 정기보고서만 사용(filter.latest_report).★
방법은 dart_filter_effectiveness와 동일(crit/warn/clean 버킷 forward수익·부트스트랩CI·양분할·결정규칙).
사용: python -m backtest.dart_distress_effectiveness
"""
from __future__ import annotations
import json, os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, ".")
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402
from backtest.dart_filter_effectiveness import build_forward_returns, bucket_stats, boot_ci, fisher  # noqa: E402
from app.dart.client import corp_code_map  # noqa: E402
from app.dart import filter as DF  # noqa: E402

ENTRIES = "/tmp/mom_entries.csv"
CACHE = "data/cache/dart_distress_pit.json"


def label_entries(fr, cmap):
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    todo = [(r["ticker"], str(r["signal_date"])) for _, r in fr.iterrows()
            if f"{r['ticker']}_{r['signal_date']}" not in cache]
    print(f"재무 부실 판정: 신규 {len(todo)}건(캐시 {len(cache)})", file=sys.stderr)
    for k, (tk, sd) in enumerate(todo):
        key = f"{tk}_{sd}"
        corp = cmap.get(str(tk).zfill(6))
        if not corp:
            cache[key] = {"level": None}; continue
        try:
            rep = DF.latest_report(corp, sd)               # 룩어헤드 차단된 최근 정기보고서
            cache[key] = DF.financial_distress(corp, rep[0], rep[1]) if rep else {"level": None, "flags": []}
        except Exception as e:
            print(f"  ! {tk} {sd}: {str(e)[:60]}", file=sys.stderr); cache[key] = {"level": None}
        time.sleep(0.05)
        if (k + 1) % 100 == 0:
            json.dump(cache, open(CACHE, "w"), ensure_ascii=False); print(f"  ...{k+1}/{len(todo)}", file=sys.stderr)
    json.dump(cache, open(CACHE, "w"), ensure_ascii=False)
    return cache


def report(fr, labels, title):
    fr = fr.copy(); fr["bucket"] = labels
    prim = fr[~fr["truncated"]]
    print(f"\n{'='*60}\n[{title}] 비절단 {len(prim)}건")
    for b in ["clean", "warn", "crit"]:
        s = bucket_stats(prim[prim["bucket"] == b])
        if s:
            print(f"  {b:5} N={s['N']:4} | 중앙 {s['median']:+6.1%} 평균 {s['mean']:+6.1%} | "
                  f"P(<−20%) {s['p_lt20']:5.1%} P(<−30%) {s['p_lt30']:5.1%} 전손 {s['p_writeoff']:5.1%} | 승률 {s['win']:4.0%}")
        else:
            print(f"  {b:5} N=0")
    cr = prim[prim["bucket"] == "crit"]["ret"].to_numpy()
    cl = prim[prim["bucket"] == "clean"]["ret"].to_numpy()
    dist = prim[prim["bucket"].isin(["crit", "warn"])]["ret"].to_numpy()   # 부실 전체(crit+warn)
    if len(cr) >= 2 and len(cl) >= 2:
        lo, mid, hi = boot_ci(cr, cl); fp = fisher(cr, cl)
        print(f"  crit−clean P(<−20%): {mid:+.1%} [95%CI {lo:+.1%}~{hi:+.1%}]" + (f" · Fisher p={fp:.3f}" if fp else ""))
    if len(dist) >= 2 and len(cl) >= 2:
        lo2, mid2, _ = boot_ci(dist, cl)
        print(f"  부실(crit+warn)−clean P(<−20%): {mid2:+.1%} [CI하한 {lo2:+.1%}] (N부실 {len(dist)})")
    return prim


def main():
    entries = pd.read_csv(ENTRIES, dtype={"signal_date": str, "entry_date": str, "ticker": str})
    panel = load_adjusted()
    fr, cal, idx = build_forward_returns(entries, panel)
    cmap = corp_code_map()
    cache = label_entries(fr, cmap)
    labels = [(cache.get(f"{r['ticker']}_{r['signal_date']}", {}).get("level") or "clean") for _, r in fr.iterrows()]
    prim = report(fr, labels, "재무 부실 vs clean (40일 forward)")

    # 양분할
    fr2 = fr.copy(); fr2["bucket"] = labels
    _sd = sorted(fr2["signal_date"]); med = _sd[len(_sd) // 2]
    print(f"\n[양분할] 중앙 {med}")
    half_ok = True
    for nm, sub in [("전반", fr2[(fr2["signal_date"] <= med) & ~fr2["truncated"]]),
                    ("후반", fr2[(fr2["signal_date"] > med) & ~fr2["truncated"]])]:
        d = sub[sub["bucket"].isin(["crit", "warn"])]; c = sub[sub["bucket"] == "clean"]
        if len(d) and len(c):
            dp = (d["ret"].to_numpy() < -0.20).mean() - (c["ret"].to_numpy() < -0.20).mean()
            print(f"  {nm}: 부실 N={len(d)} clean N={len(c)} | ΔP(<−20%) {dp:+.1%}")
            if dp <= 0: half_ok = False
        else:
            print(f"  {nm}: 부실 N={len(d)} — 표본부족"); half_ok = False

    # 결정규칙(부실=crit+warn 기준; 표본 작을 것)
    fr2 = fr2[~fr2["truncated"]]
    dist = fr2[fr2["bucket"].isin(["crit", "warn"])]["ret"].to_numpy()
    cl = fr2[fr2["bucket"] == "clean"]["ret"].to_numpy()
    print(f"\n[결정규칙 — 부실(crit+warn) 회피]")
    if len(dist) < 2 or len(cl) < 2:
        print("  표본 부족 → 증거 불충분, annotate-only 유지."); return
    lo, _, _ = boot_ci(dist, cl)
    p_d = float((dist < -0.20).mean()); p_c = float((cl < -0.20).mean())
    a = len(dist) >= 15; b = (p_d >= 2 * p_c) and (p_d - p_c >= 0.15) and (lo > 0)
    cc = float(np.median(dist)) <= float(np.median(cl))
    print(f"  (a) 부실 N≥15: {a} (N={len(dist)})")
    print(f"  (b) P(<−20%)≥2×clean & 격차≥15%p & CI하한>0: {b} (부실 {p_d:.1%} vs clean {p_c:.1%}, CI하한 {lo:+.1%})")
    print(f"  (c) 부실 중앙값≤clean: {cc} ({np.median(dist):+.1%} vs {np.median(cl):+.1%})")
    print(f"  (d) 양반기 부호 일치: {half_ok}")
    ok = a and b and cc and half_ok
    print(f"\n  ▶ 판정: {'★게이트 통과 → 라이브 재무부실 제외 배선★' if ok else '게이트 미통과 → annotate-only(화면 경고)만, 표본누적 후 재검증'}")


if __name__ == "__main__":
    main()
