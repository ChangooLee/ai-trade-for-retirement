"""PIT 동결 터미널공시 테이블 — pit_mktcap_backtest --dart-table 용.
후보종목(/tmp/mom_cands.csv)별로 DART 이력에서 is_terminal 공시일(rcept_dt)만 추출 → {ticker:[YYYYMMDD,...]}.
후보로 등장한 연도(+직전해 경계)만 조회해 호출 절감. 캐시·재개 가능. 룩어헤드는 백테스트 쪽 N일 윈도우로 차단.
사용: python -m backtest.build_dart_terminal_table [--cands /tmp/mom_cands.csv]
"""
from __future__ import annotations
import argparse, json, os, sys, time
import pandas as pd
sys.path.insert(0, ".")
from app.dart.client import corp_code_map  # noqa: E402
from app.dart import filter as DF  # noqa: E402

OUT = "data/cache/dart_terminal_pit.json"


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--cands", default="/tmp/mom_cands.csv")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    OUT = a.out
    cands = pd.read_csv(a.cands, dtype={"signal_date": str, "ticker": str})
    cands["year"] = cands["signal_date"].str[:4].astype(int)
    cmap = corp_code_map()
    tbl = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    groups = list(cands.groupby("ticker"))
    print(f"터미널 테이블 구축: {len(groups)}종목(캐시 {len(tbl)})", file=sys.stderr)
    calls = 0
    for k, (tk, g) in enumerate(groups):
        if tk in tbl:
            continue
        corp = cmap.get(str(tk).zfill(6))
        if not corp:
            tbl[tk] = []; continue
        years = set()
        for y in g["year"].unique():
            years.add(int(y)); years.add(int(y) - 1)        # 직전해(룩백 경계) 포함
        terms = set()
        for y in sorted(years):
            try:
                # 하이브리드: DS005 구조화(부도/영업정지/회생/해산/채권관리·자기회사) + list.json 제목(상폐/정지/실질심사·우선주/제3자 FP 가드)
                terms.update(DF.terminal_events(corp, f"{y}0101", f"{y}1231"))
            except Exception as e:
                print(f"  {tk} {y} 실패: {str(e)[:50]}", file=sys.stderr)
            calls += 1; time.sleep(0.05)
        tbl[tk] = sorted(terms)
        if (k + 1) % 50 == 0:
            json.dump(tbl, open(OUT, "w"), ensure_ascii=False)
            print(f"  ...{k+1}/{len(groups)} (호출 {calls})", file=sys.stderr)
    json.dump(tbl, open(OUT, "w"), ensure_ascii=False)
    n_term = sum(1 for v in tbl.values() if v)
    n_ev = sum(len(v) for v in tbl.values())
    print(f"완료: {len(tbl)}종목 · 터미널보유 {n_term}종목 · 총 터미널이벤트 {n_ev}건 → {OUT}")


if __name__ == "__main__":
    main()
