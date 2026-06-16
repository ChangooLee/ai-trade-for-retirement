"""검증 게이트 — DART 악재 필터가 실제로 손실을 회피하나? (라이브 배선 前 결정적 검증)

질문: 신호일 직전 N거래일에 '악재 공시(crit/warn)'가 있던 진입이, 없던(clean) 진입보다
      40일 보유 forward 수익이 나쁘고 꼬리손실(−20%↓)이 잦은가?
방법(설계 워크플로 2026-06-16 backtest_spec 준수, 룩어헤드 차단):
  - 진입: /tmp/mom_entries.csv (검증된 전략 실제 진입 553건; signal_date/entry_date/ticker/assumed_open)
  - 공시: app.dart.client.disclosures(corp, bgn, signal_date) — ★window end=신호일★, rcept_dt<=신호일 재확인,
          종목별 1회 조회 캐시(data/cache/dart_pit_effectiveness.json) 후 동결.
  - forward 수익: 진입일 수정시가 매수, 40거래일 후 첫 유효 수정시가 청산(없으면 전손 −1.0), 왕복비용 0.35%.
  - 버킷별(clean/warn/crit) N·중앙값·평균·P(<−20%)·P(<−30%)·P(전손)·최악10%·승률 + crit 최악10건.
  - 효과크기: crit−clean의 P(<−20%) 차이 부트스트랩 95% CI(10k) + Fisher exact.
  - 강건성: N∈{20,30,40}; 헤드라인=N30 crit-only; 양분할(신호일 중앙값) 부호 일치 필수.
결정규칙(라이브 crit 제외 채택): (a)crit N≥15 (b)crit P(<−20%)≥2×clean & 격차≥15%p & CI하한>0
  (c)crit 중앙값≤clean (d)양반기 부호 일치. 미달 시 '증거 불충분' → annotate-only 유지(배선 안 함).
사용: python -m backtest.dart_filter_effectiveness
"""
from __future__ import annotations
import json, os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, ".")
from backtest.pit_mktcap_backtest import load_adjusted  # noqa: E402
from app.dart.client import corp_code_map, disclosures  # noqa: E402
from app.dart import filter as DF  # noqa: E402

ENTRIES = "/tmp/mom_entries.csv"
CACHE = "data/cache/dart_pit_effectiveness.json"
H = 40                # 보유 거래일(전략 시간청산)
COST = 0.0035         # 왕복비용
NMAX = 40             # 최대 룩백(거래일) — 1회 조회로 N20/30/40 파생
NS = [20, 30, 40]


def build_forward_returns(entries, panel):
    """진입별 40일 forward 수익(PIT, 수정시가, 전손 −1.0). truncated(패널끝) 분리."""
    cal = sorted(panel["date"].unique())
    idx = {d: i for i, d in enumerate(cal)}
    opn = panel.pivot_table(index="date", columns="ticker", values="open")
    n = len(cal)
    rows = []
    for _, r in entries.iterrows():
        tk = r["ticker"]; ed = pd.Timestamp(str(r["entry_date"]))
        ei = idx.get(ed)
        if ei is None or tk not in opn.columns:
            continue
        buy = float(r["assumed_open"])
        if not (buy > 0):
            continue
        truncated = (ei + H) > (n - 1)
        ex = 0.0
        for j in range(ei + H, min(ei + H + 41, n)):
            v = opn.iloc[j].get(tk)
            if v is not None and np.isfinite(v) and v > 0:
                ex = float(v); break
        ret = (ex / buy - 1 - COST) if ex > 0 else -1.0     # 청산불가=전손(상폐/정지)
        rows.append({"ticker": tk, "signal_date": str(r["signal_date"]),
                     "entry_date": str(r["entry_date"]), "ret": ret,
                     "writeoff": ex <= 0, "truncated": bool(truncated)})
    return pd.DataFrame(rows), cal, idx


def fetch_disclosures(entries, cal, idx):
    """진입별 [신호일−NMAX거래일 ~ 신호일] 공시 분류 결과 캐시. {key: [[rcept_dt, kind], ...]}."""
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    cmap = corp_code_map()
    todo = []
    for _, r in entries.iterrows():
        key = f"{r['ticker']}_{r['signal_date']}"
        if key not in cache:
            todo.append((r["ticker"], str(r["signal_date"])))
    print(f"DART 조회: 신규 {len(todo)}건(캐시 {len(cache)}건)", file=sys.stderr)
    for k, (tk, sd) in enumerate(todo):
        corp = cmap.get(str(tk).zfill(6))
        key = f"{tk}_{sd}"
        if not corp:
            cache[key] = []; continue
        si = idx.get(pd.Timestamp(sd))
        bgn = cal[max(0, si - NMAX)].strftime("%Y%m%d") if si is not None else sd
        try:
            items = disclosures(corp, bgn, sd)
            kept = []
            for it in items:
                rd = it["rcept_dt"]
                if not rd or rd > sd:               # 룩어헤드 차단(신호일 이후 제외)
                    continue
                kind = DF.classify(it["report_nm"])
                if kind:
                    kept.append([rd, kind])
            cache[key] = kept
        except Exception as e:
            print(f"  ! {tk} {sd} 실패: {str(e)[:60]}", file=sys.stderr); cache[key] = []
        time.sleep(0.08)
        if (k + 1) % 100 == 0:
            json.dump(cache, open(CACHE, "w"), ensure_ascii=False)
            print(f"  ...{k+1}/{len(todo)}", file=sys.stderr)
    json.dump(cache, open(CACHE, "w"), ensure_ascii=False)
    return cache


def label_for_N(cache, cal, idx, ticker, signal_date, N):
    """신호일−N거래일 이후 공시만으로 라벨. crit > warn > clean."""
    si = idx.get(pd.Timestamp(signal_date))
    bgn = cal[max(0, si - N)].strftime("%Y%m%d") if si is not None else "00000000"
    items = cache.get(f"{ticker}_{signal_date}", [])
    kinds = {kind for rd, kind in items if bgn <= rd <= signal_date}
    return "crit" if "crit" in kinds else ("warn" if "warn" in kinds else "clean")


def bucket_stats(df):
    r = df["ret"].to_numpy()
    if len(r) == 0:
        return None
    dec = np.sort(r)[:max(1, len(r) // 10)]
    return {"N": len(r), "median": float(np.median(r)), "mean": float(r.mean()),
            "p_lt20": float((r < -0.20).mean()), "p_lt30": float((r < -0.30).mean()),
            "p_writeoff": float(df["writeoff"].mean()), "worst_decile": float(dec.mean()),
            "win": float((r > 0).mean())}


def boot_ci(crit_r, clean_r, iters=10000, seed=0):
    """crit−clean의 P(<−20%) 차이 부트스트랩 95% CI."""
    if len(crit_r) < 2 or len(clean_r) < 2:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    diffs = np.empty(iters)
    cr = (crit_r < -0.20).astype(float); cl = (clean_r < -0.20).astype(float)
    for i in range(iters):
        a = rng.choice(cr, len(cr)); b = rng.choice(cl, len(cl))
        diffs[i] = a.mean() - b.mean()
    return float(np.percentile(diffs, 2.5)), float(diffs.mean()), float(np.percentile(diffs, 97.5))


def fisher(crit_r, clean_r):
    try:
        from scipy.stats import fisher_exact
        a = int((crit_r < -0.20).sum()); b = len(crit_r) - a
        c = int((clean_r < -0.20).sum()); d = len(clean_r) - c
        return float(fisher_exact([[a, b], [c, d]], alternative="greater")[1])
    except Exception:
        return None


def report(label, fr, lab):
    fr = fr.copy(); fr["bucket"] = lab
    prim = fr[~fr["truncated"]]
    print(f"\n{'='*64}\n[{label}] 비절단(완료) {len(prim)}건 / 절단 {int(fr['truncated'].sum())}건 제외")
    buckets = {}
    for b in ["clean", "warn", "crit"]:
        s = bucket_stats(prim[prim["bucket"] == b])
        buckets[b] = s
        if s:
            print(f"  {b:5} N={s['N']:4} | 중앙 {s['median']:+6.1%} 평균 {s['mean']:+6.1%} | "
                  f"P(<−20%) {s['p_lt20']:5.1%} P(<−30%) {s['p_lt30']:5.1%} 전손 {s['p_writeoff']:5.1%} | "
                  f"최악10% {s['worst_decile']:+6.1%} 승률 {s['win']:4.0%}")
        else:
            print(f"  {b:5} N=0")
    crit_r = prim[prim["bucket"] == "crit"]["ret"].to_numpy()
    clean_r = prim[prim["bucket"] == "clean"]["ret"].to_numpy()
    if len(crit_r) >= 2 and len(clean_r) >= 2:
        lo, mid, hi = boot_ci(crit_r, clean_r)
        fp = fisher(crit_r, clean_r)
        print(f"  crit−clean P(<−20%) 차이: {mid:+.1%} [95%CI {lo:+.1%}~{hi:+.1%}]"
              + (f" · Fisher p={fp:.3f}" if fp is not None else ""))
    return buckets, prim


def main():
    entries = pd.read_csv(ENTRIES, dtype={"signal_date": str, "entry_date": str, "ticker": str})
    panel = load_adjusted()
    fr, cal, idx = build_forward_returns(entries, panel)
    print(f"forward 수익 산출: {len(fr)}/{len(entries)}건 (truncated {int(fr['truncated'].sum())})", file=sys.stderr)
    cache = fetch_disclosures(fr, cal, idx)

    # 헤드라인 N=30 crit-only + 강건성 N∈{20,40}
    headline = None
    for N in NS:
        lab = [label_for_N(cache, cal, idx, r["ticker"], r["signal_date"], N) for _, r in fr.iterrows()]
        buckets, prim = report(f"N={N}거래일 룩백", fr, lab)
        if N == 30:
            headline = (buckets, prim, lab)

    # 양분할(신호일 중앙값) — 헤드라인 N=30
    buckets, prim, lab30 = headline
    fr30 = fr.copy(); fr30["bucket"] = lab30
    _sd = sorted(fr30["signal_date"]); med = _sd[len(_sd) // 2]      # 문자열 중앙값(YYYYMMDD 정렬)
    print(f"\n{'='*64}\n[양분할 N=30 robust] 분할 신호일 중앙값 {med}")
    half_ok = True
    for name, sub in [("전반", fr30[(fr30["signal_date"] <= med) & ~fr30["truncated"]]),
                      ("후반", fr30[(fr30["signal_date"] > med) & ~fr30["truncated"]])]:
        c = sub[sub["bucket"] == "crit"]; cl = sub[sub["bucket"] == "clean"]
        if len(c) and len(cl):
            dp = (c["ret"].to_numpy() < -0.20).mean() - (cl["ret"].to_numpy() < -0.20).mean()
            dm = c["ret"].median() - cl["ret"].median()
            print(f"  {name}: crit N={len(c)} clean N={len(cl)} | ΔP(<−20%) {dp:+.1%} · Δ중앙 {dm:+.1%}")
            if dp <= 0:
                half_ok = False
        else:
            print(f"  {name}: crit N={len(c)} clean N={len(cl)} — 표본부족"); half_ok = False

    # crit 최악 10건(휴먼 감사)
    crit_tr = prim[prim["bucket"] == "crit"].nsmallest(10, "ret")
    print(f"\n[crit 최악 10건]")
    cmap = corp_code_map()
    for _, t in crit_tr.iterrows():
        items = cache.get(f"{t['ticker']}_{t['signal_date']}", [])
        crits = [rd for rd, k in items if k == "crit"]
        print(f"  {t['ticker']} 신호{t['signal_date']} ret {t['ret']:+.1%}{' 전손' if t['writeoff'] else ''} · crit공시일 {crits[:2]}")

    # 결정규칙
    c, cl = buckets.get("crit"), buckets.get("clean")
    print(f"\n{'='*64}\n[결정규칙 — 헤드라인 N=30 crit-only]")
    if not c or not cl:
        print("  crit 또는 clean 표본 없음 → 증거 불충분 → annotate-only 유지."); return
    lo, _, _ = boot_ci(prim[prim["bucket"] == "crit"]["ret"].to_numpy(),
                       prim[prim["bucket"] == "clean"]["ret"].to_numpy())
    a = c["N"] >= 15
    b = (c["p_lt20"] >= 2 * cl["p_lt20"]) and (c["p_lt20"] - cl["p_lt20"] >= 0.15) and (lo > 0)
    cc = c["median"] <= cl["median"]
    print(f"  (a) crit N≥15: {a} (N={c['N']})")
    print(f"  (b) P(<−20%)≥2×clean & 격차≥15%p & CI하한>0: {b} "
          f"(crit {c['p_lt20']:.1%} vs clean {cl['p_lt20']:.1%}, CI하한 {lo:+.1%})")
    print(f"  (c) crit 중앙값≤clean: {cc} ({c['median']:+.1%} vs {cl['median']:+.1%})")
    print(f"  (d) 양반기 부호 일치: {half_ok}")
    verdict = a and b and cc and half_ok
    print(f"\n  ▶ 판정: {'★게이트 통과 → 라이브 crit 매수 제외 배선 권고★' if verdict else '게이트 미통과 → 증거 불충분, annotate-only 유지(배선 안 함)'}")


if __name__ == "__main__":
    main()
