"""reco_archive.build_full_history 검증 — (1) 실데이터 회귀·data_stale·관망 (2) 합성 stale 경로 단위테스트.
사용: .venv/bin/python scripts/_reco_verify.py
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd, yaml
from app.data import krx_loader as L
from app.indicators.weekly import to_weekly, add_weekly_indicators
from app.batch import reco_archive

cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))
daily = L.load_daily_ohlcv(cfg["paths"]["daily_ohlcv"])
asof = daily["date"].max()
weekly_ind = add_weekly_indicators(to_weekly(daily[daily["date"] <= asof]),
                                   cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
bt = json.load(open("state/bt_days.json", encoding="utf-8"))
btp = pd.read_parquet("state/bt_prices.parquet")
last = sorted(bt["days"])[-1]
buy_order = bt["days"][last].get("buy", [])
reco = reco_archive.build_full_history(bt["days"], btp, weekly_ind, set(buy_order),
                                       str(asof.date()), bt.get("names", {}))
json.dump(reco, open("/tmp/reco_full_new.json", "w", encoding="utf-8"), ensure_ascii=False, default=str)

psk = next((h for h in reco if h["ticker"] == "031980"), None)
print("=== (1) 실데이터 ===")
if psk:
    print(f"  PSK: reco_date={psk['reco_date']} status={psk['status']} ret={psk['ret']:.3f} held_cont={psk['held_continuous']} "
          f"data_stale={psk['data_stale']} n_ep={psk['n_episodes']} (회귀기준: 2026-06-05 / 추천중 / 0.317 / True)")
n_stale = sum(1 for h in reco if h.get("data_stale"))
watch = [(h["ticker"], h["name"][:10], round((h["ret"] or 0) * 100, 1)) for h in reco if h["held_continuous"] and not h["active"]]
print(f"  data_stale 종목: {n_stale}개 (현 아카이브는 현재 상장 기준이라 0 예상)")
print(f"  관망(held_continuous & !active): {len(watch)}개 → {watch}")

# 회귀: 이전 덤프(/tmp/reco_full.json)와 ret 비교
try:
    old = {h["ticker"]: h.get("ret") for h in json.load(open("/tmp/reco_full.json", encoding="utf-8"))}
    new = {h["ticker"]: h.get("ret") for h in reco}
    diffs = [tk for tk in new if tk in old and (old[tk] or 0) != (new[tk] or 0)]
    print(f"  회귀(이전 덤프 대비 ret 변동): {len(diffs)}종목 {diffs[:10]}")
except FileNotFoundError:
    print("  (이전 덤프 없음 — 회귀 비교 생략)")

print("\n=== (2) 합성 stale 단위테스트 (종목 데이터가 asof 전에 종료) ===")
cal_dates = [f"2026-01-{d:02d}" for d in range(1, 21)]
days = {d: {"buy": [], "sells": []} for d in cal_dates}
days["2026-01-02"]["buy"] = ["TEST"]
# TEST 가격은 01-05까지만 존재 → 이후 유니버스 이탈(상폐 가능)
prices_df = pd.DataFrame([{"date": d, "ticker": "TEST", "close": 1000 + i * 10} for i, d in enumerate(cal_dates[:5])])
r = reco_archive.build_full_history(days, prices_df, None, set(), "2026-01-20", {"TEST": "테스트"}, hold_days=40, gap_days=6)
t = r[0] if r else {}
print(f"  status={t.get('status')} data_stale={t.get('data_stale')} ret={t.get('ret')} exit_reason={t.get('episodes',[{}])[0].get('exit_reason')}")
ok = t.get("status") == "데이터종료" and t.get("data_stale") is True and t.get("episodes", [{}])[0].get("stale") is True
print(f"  → {'✅ stale 경로 정상(데이터종료·플래그·수익 보존)' if ok else '❌ stale 경로 실패'}")
