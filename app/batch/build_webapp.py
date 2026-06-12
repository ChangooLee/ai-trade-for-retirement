"""데일리 트레이드 웹앱 빌드 — 서버에서 유니버스/신호/노출/차트데이터를 JSON으로 임베드.

보유/포트폴리오는 클라이언트(localStorage)에서 관리하므로 서버는 포지션을 다루지 않는다.
산출: backtest/trade.html (자체완결 — 임베드 데이터 + 클라이언트 JS).
사용: python -m app.batch.build_webapp --asof 20260605 --out backtest/trade.html
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sys

import pandas as pd
import yaml

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
from app.data import krx_api as _krx  # noqa: E402


def resolve_latest_official(auth, today_str, back=8):
    """시스템 날짜에서 역순으로 공식 API에 EOD 데이터가 있는 최신 거래일을 찾는다."""
    c = _krx.KRXOpenAPIClient(auth_key=auth, timeout=20)
    base = dt.datetime.strptime(today_str, "%Y%m%d")
    for i in range(back):
        bd = (base - dt.timedelta(days=i)).strftime("%Y%m%d")
        try:
            if c.call("stk_bydd_trd", bd):
                return bd
        except Exception:
            pass
    return today_str


def fetch_all_stocks(auth, asof_str):
    """전 상장종목(코스피+코스닥) 코드→[이름, 종가, 시장] — 검색·시세용(유니버스 밖 포함)."""
    import re as _re
    c = _krx.KRXOpenAPIClient(auth_key=auth, timeout=20)
    out = {}
    for mk, ep in (("KOSPI", "stk_bydd_trd"), ("KOSDAQ", "ksq_bydd_trd")):
        try:
            recs = c.call(ep, asof_str)
        except Exception:
            recs = []
        for d in recs:
            code = str(d.get("ISU_CD", "")).strip()
            nm = d.get("ISU_NM", "")
            if not (code.isdigit() and len(code) == 6) or not nm:
                continue
            if _re.search(r"우$|우B|스팩|[0-9]호$", nm):
                continue
            try:
                close = float(str(d.get("TDD_CLSPRC", "0")).replace(",", "") or 0)
            except ValueError:
                close = 0.0
            if close > 0:
                out[code] = [nm, int(round(close)), mk]
    return out

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from app.data import krx_loader as L  # noqa: E402
from app.data.calendar import resolve_asof, next_trading_day, trading_calendar, last_completed_week_cutoff  # noqa: E402
from app.indicators.daily import add_daily_indicators  # noqa: E402
from app.indicators.weekly import to_weekly, add_weekly_indicators  # noqa: E402
from app.indicators.regime import compute_d4_exposure  # noqa: E402
from app.indicators.leader import compute_leader_flags  # noqa: E402
from app.indicators.pullback import compute_pullback_flags  # noqa: E402
from app.indicators.tda import compute_tda_signals, tda_buy_sell, HAS_RIPSER  # noqa: E402
from app.portfolio import ledger  # noqa: E402
from app.batch.backtest_stats import BACKTEST  # noqa: E402
from app.dart.filter import annotate_tickers  # noqa: E402

from app.render.sectors import _sector  # noqa: E402
from app.data.fetchers import load_krx_auth_key  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "render", "templates", "trade_app.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None)
    ap.add_argument("--config", default="config/strategy.yaml")
    ap.add_argument("--out", default="backtest/trade.html")
    ap.add_argument("--from", dest="fromdate", default="20160101")
    ap.add_argument("--chart-days", type=int, default=100)
    ap.add_argument("--refresh", action="store_true", help="parquet 캐시 재수집(최신 데이터)")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    dpath, ipath = cfg["paths"]["daily_ohlcv"], cfg["paths"]["index_ohlcv"]
    auth = load_krx_auth_key()
    today = dt.date.today().strftime("%Y%m%d")             # 시스템 날짜
    asof_str = args.asof or resolve_latest_official(auth, today)   # 최신 거래일(EOD 발표분)
    if args.refresh or not os.path.exists(dpath):
        L.build_daily_ohlcv(asof_str, args.fromdate, cfg["universe"]["top_n_by_trdval20"], dpath)
    if args.refresh or not os.path.exists(ipath):
        L.build_index_ohlcv(asof_str, args.fromdate, ipath)
    daily = L.load_daily_ohlcv(dpath)
    index = L.load_index_ohlcv(ipath)
    asof, _ = resolve_asof(asof_str, daily)
    daily = daily[daily["date"] <= asof].copy()
    index = index[index["date"] <= asof].copy()
    cal = [str(pd.Timestamp(d).date()) for d in trading_calendar(daily)][-260:]
    nxt = next_trading_day(trading_calendar(daily), asof)

    daily_ind = add_daily_indicators(daily)
    weekly_ind = add_weekly_indicators(to_weekly(daily), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
    daily_asof = daily_ind.sort_values(["ticker", "date"]).groupby("ticker").tail(1).copy()
    wk_cut = last_completed_week_cutoff(asof)   # 주중이면 진행 중 주 제외 → 직전 완성주 사용
    weekly_asof = weekly_ind[weekly_ind["week_end"] <= wk_cut].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
    exp = compute_d4_exposure(index, asof, cfg)

    uni = daily_asof[(daily_asof["close"] >= cfg["universe"]["min_close"]) &
                     (daily_asof["listing_days"] >= cfg["universe"]["min_listing_days"])].copy()
    managed = [str(t).zfill(6) for t in cfg["universe"].get("managed_tickers", [])]
    if managed:   # 관리 지정 종목은 거래대금 순위·필터 무관하게 강제 포함(풀 분석)
        extra = daily_asof[daily_asof["ticker"].isin(managed) & ~daily_asof["ticker"].isin(uni["ticker"])]
        if len(extra):
            uni = pd.concat([uni, extra], ignore_index=True)
    leaders = compute_leader_flags(uni, cfg)
    pulls = compute_pullback_flags(weekly_asof, cfg)
    merged = leaders.merge(pulls[["ticker", "pullback_20w_105", "dist_wma20", "w_ma20"]], on="ticker", how="left")
    cand = ledger.build_buy_candidates(merged, pd.DataFrame(), pd.DataFrame(), cfg)
    buy_order = list(cand.head(20)["ticker"]) if len(cand) else []
    score_map = dict(zip(cand["ticker"], cand["leader_score"])) if len(cand) else {}

    # 기존 방식 이탈 스크린: 20주선 하향 + 6-1M 모멘텀 약화 (추세 붕괴 = 회피/청산)
    leg = merged.copy()
    leg["below_w"] = leg["close"] / leg["w_ma20"] - 1
    legacy_sell = list(leg[(leg["below_w"] < 0) & (leg["mom_6m_1m"] < 0)]
                       .sort_values("below_w").head(8)["ticker"])

    # TDA(위상수학적) 종목 발굴 — 기존 방식과 독립
    tcfg = cfg.get("tda", {})
    tda_df = compute_tda_signals(daily_ind[daily_ind["ticker"].isin(set(uni["ticker"]))], asof, cfg)
    tda_buy, tda_sell = tda_buy_sell(tda_df, tcfg.get("n_buy", 8), tcfg.get("n_sell", 8))
    tda_map = {row["ticker"]: row for _, row in tda_df.iterrows()} if len(tda_df) else {}

    # 종목 데이터 (유니버스 전부, 검색·차트용)
    g = daily_ind.groupby("ticker")
    mi = merged.set_index("ticker")
    buy_set = set(buy_order)
    stocks = {}
    for tk in uni["ticker"]:
        if tk not in mi.index:
            continue
        r = mi.loc[tk]
        sub = g.get_group(tk).sort_values("date").tail(args.chart_days)
        ohlc = [[int(round(v)) for v in row] for row in sub[["open", "high", "low", "close", "volume"]].values]
        sw = r["swing_low20"]
        stocks[tk] = {
            "name": r["name"], "market": r["market"], "close": float(r["close"]),
            "rs": float(r.get("rs_rank") or 0), "high52": float(r["high_52w_ratio"]),
            "dist_wma20": float(r["dist_wma20"]) if pd.notna(r.get("dist_wma20")) else 0.0,
            "atv20": float(r["avg_trdval20"]), "pullback": bool(r.get("pullback_20w_105")),
            "leader": bool(r["is_f_leader"]), "buy": tk in buy_set,
            "score": round(float(score_map.get(tk, 0)), 1), "swing20": float(sw) if pd.notna(sw) else None,
            "wma20": float(r["w_ma20"]) if pd.notna(r.get("w_ma20")) else None,
            "sector": _sector(tk), "daily": ohlc,
        }
        tr = tda_map.get(tk)
        if tr is not None:
            stocks[tk]["tda"] = {
                "risk": round(float(tr["risk"]), 2), "dir": round(float(tr["dir"]), 2),
                "score": round(float(tr["score"]), 2), "risk_pct": round(float(tr["risk_pct"]), 3),
                "pl_norm": round(float(tr["pl_norm"]), 5), "entropy": round(float(tr["pers_entropy"]), 3),
                "turb": None if pd.isna(tr["turb"]) else round(float(tr["turb"]), 4),
                "rising": bool(tr["d_norm"] > 0),
            }

    # 오버나이트 단타 (검증: backtest/shortterm_*. 종가 매수 → 익일 시가 매도. 통계는 매 빌드 실데이터 재계산)
    ds = daily.sort_values(["ticker", "name"]).sort_values(["ticker", "date"])
    gg = ds.groupby("ticker")
    ds["ret1"] = gg["close"].pct_change()
    ds["vol20"] = gg["volume"].transform(lambda s: s.rolling(20).mean())
    ds["tv20"] = gg["trdval"].transform(lambda s: s.rolling(20).mean())
    ds["next_open"] = gg["open"].shift(-1)
    hist = ds[(ds["close"] > 0) & (ds["next_open"] > 0) & (ds["tv20"] > 1e9) &
              (ds["volume"] > 3 * ds["vol20"]) & (ds["ret1"] > 0.03) & (ds["ret1"] < 0.285)].copy()
    hist["r_on"] = hist["next_open"] / hist["close"] - 1
    on_stats = {}
    if len(hist) > 100:
        r = hist["r_on"]
        on_stats = {"n": int(len(r)), "mean": round(float(r.mean()), 5), "median": round(float(r.median()), 5),
                    "win": round(float((r > 0).mean()), 3), "p05": round(float(r.quantile(0.05)), 4),
                    "p95": round(float(r.quantile(0.95)), 4),
                    "from": str(hist["date"].min().date())}
        # D4 국면별 분해 (검증: Risk-Off 고변동 밤에만 net 양(+) — 유동성 공급 보상. 주 단위 캐시)
        wkk = hist["date"].dt.to_period("W").astype(str)
        mcache = {}
        for w, d in zip(wkk, hist["date"]):
            if w not in mcache:
                try:
                    mcache[w] = compute_d4_exposure(index[index["date"] <= d], d, cfg)["mode"]
                except Exception:
                    mcache[w] = "?"
        hm = pd.Series([mcache[w] for w in wkk], index=hist.index)
        nightly = hist.groupby("date")["r_on"].mean()                  # 밤 단위(날짜 군집 보정)
        nmode = hm.groupby(hist["date"]).first().reindex(nightly.index)
        noff = nightly[nmode == "Risk-Off"]; nnon = nightly[nmode.isin(("Risk-On", "Half"))]
        if len(noff) > 50 and len(nnon) > 50:
            on_stats["off_mean"] = round(float(noff.mean()), 5); on_stats["off_win"] = round(float((noff > 0.0035).mean()), 3)
            on_stats["non_mean"] = round(float(nnon.mean()), 5); on_stats["non_win"] = round(float((nnon > 0.0035).mean()), 3)
    la = ds[ds["date"] == asof]
    on_base = la[(la["tv20"] > 1e9) & la["vol20"].notna() & (la["close"] > 0)]
    on_sig = on_base[(on_base["volume"] > 3 * on_base["vol20"]) & (on_base["ret1"] > 0.03) & (on_base["ret1"] < 0.285)]
    on_watch = on_base[(on_base["volume"] > 2 * on_base["vol20"]) & (on_base["ret1"] > 0) & (on_base["ret1"] < 0.285)]
    sig_set = set(on_sig["ticker"])
    def _on_rows(sub, k):
        sub = sub.sort_values("trdval", ascending=False).head(k)
        return [{"ticker": r["ticker"], "name": r["name"], "close": float(r["close"]),
                 "ret1": round(float(r["ret1"]), 4), "vr": round(float(r["volume"] / r["vol20"]), 1),
                 "tv": round(float(r["trdval"]) / 1e8), "resig": r["ticker"] in sig_set}
                for _, r in sub.iterrows()]
    overnight = {"signals": _on_rows(on_sig, 8), "watch": _on_rows(on_watch, 12), "stats": on_stats}
    print(f"오버나이트: 신호 {len(on_sig)} · 워치 {len(on_watch)} · 통계 N={on_stats.get('n')}", file=sys.stderr)

    # 전 상장종목 '브로드' 분석 — 유니버스 밖 종목도 지표·TDA 신호 제공(차트는 없음).
    broad = {}
    bpath = os.path.join(_REPO, "data/cache/broad_ohlcv.parquet")
    if os.path.exists(bpath):
        bdf = pd.read_parquet(bpath); bdf["date"] = pd.to_datetime(bdf["date"])
        bdf = bdf[bdf["date"] <= asof]
        bind = add_daily_indicators(bdf)
        bwk = add_weekly_indicators(to_weekly(bdf), cfg["pullback"]["weekly_ma"], cfg["pullback"]["low_band"])
        b_asof = bind.sort_values(["ticker", "date"]).groupby("ticker").tail(1)
        bwa = bwk[bwk["week_end"] <= wk_cut].sort_values(["ticker", "week_end"]).groupby("ticker").tail(1)
        buni = b_asof[b_asof["close"] >= cfg["universe"]["min_close"]].copy()
        bmerged = compute_leader_flags(buni, cfg).merge(
            compute_pullback_flags(bwa, cfg)[["ticker", "pullback_20w_105", "dist_wma20", "w_ma20"]], on="ticker", how="left")
        btda = compute_tda_signals(bind[bind["ticker"].isin(set(buni["ticker"]))], asof, cfg)
        btda_map = {row["ticker"]: row for _, row in btda.iterrows()} if len(btda) else {}
        bmi = bmerged.set_index("ticker")
        bg = bind.groupby("ticker")
        bcd = min(args.chart_days, 80)   # 브로드는 80일 차트(페이로드 절감, 원시가)
        for tk in buni["ticker"]:
            if tk in stocks or tk not in bmi.index:   # 상위 유니버스는 stocks(풀+차트)에 이미 존재
                continue
            r = bmi.loc[tk]; sw = r["swing_low20"]
            bsub = bg.get_group(tk).sort_values("date").tail(bcd)
            bohlc = [[int(round(v)) for v in row] for row in bsub[["open", "high", "low", "close", "volume"]].values]
            ent = {"name": r["name"], "market": r["market"], "close": float(r["close"]),
                   "rs": float(r.get("rs_rank") or 0),
                   "high52": float(r["high_52w_ratio"]) if pd.notna(r.get("high_52w_ratio")) else 0.0,
                   "dist_wma20": float(r["dist_wma20"]) if pd.notna(r.get("dist_wma20")) else 0.0,
                   "pullback": bool(r.get("pullback_20w_105")), "leader": bool(r["is_f_leader"]),
                   "swing20": float(sw) if pd.notna(sw) else None,
                   "wma20": float(r["w_ma20"]) if pd.notna(r.get("w_ma20")) else None, "sector": _sector(tk),
                   "daily": bohlc}
            tr = btda_map.get(tk)
            if tr is not None:
                ent["tda"] = {"risk": round(float(tr["risk"]), 2), "dir": round(float(tr["dir"]), 2),
                              "score": round(float(tr["score"]), 2), "risk_pct": round(float(tr["risk_pct"]), 3),
                              "pl_norm": round(float(tr["pl_norm"]), 5), "entropy": round(float(tr["pers_entropy"]), 3),
                              "turb": None if pd.isna(tr["turb"]) else round(float(tr["turb"]), 4), "rising": bool(tr["d_norm"] > 0)}
            broad[tk] = ent
        print(f"브로드 분석: {len(broad)}종목(유니버스 밖, 스칼라 신호)", file=sys.stderr)

    # DART 공시 위험 주석 — 후보 종목의 최근 30일 악재 공시(상폐위험/희석) 표시
    dart_targets = list(dict.fromkeys(
        buy_order + tda_buy + tda_sell + legacy_sell +
        [r["ticker"] for r in overnight["signals"]] + [r["ticker"] for r in overnight["watch"]]))
    try:
        dart_flags = annotate_tickers(dart_targets, days=30, asof=asof_str)
        print(f"DART 공시 주석: 대상 {len(dart_targets)} · 위험 표시 {len(dart_flags)}종목", file=sys.stderr)
    except Exception as e:
        print(f"DART 주석 실패(생략): {e}", file=sys.stderr)
        dart_flags = {}

    payload = {
        "meta": {"asof": str(asof.date()), "next_day": str(nxt.date()),
                 "system_date": f"{today[:4]}-{today[4:6]}-{today[6:]}",
                 "strategy": "F 리더 + 20주선 눌림 + 8주(40거래일) 보유 + D4 변동성 노출",
                 "universe_count": int(len(uni)), "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "universe_rule": (f"KOSPI+KOSDAQ · 최근 20거래일 평균 거래대금 상위 {cfg['universe']['top_n_by_trdval20']}"
                                   f" · 종가 ≥ {cfg['universe']['min_close']:,}원 · 상장 ≥ {cfg['universe']['min_listing_days']}거래일"
                                   f" · 우선주·스팩 제외" + (f" · 관리 지정 {len(managed)}종목 상시 포함" if managed else "")),
                 "managed_count": len(managed)},
        "exposure": {"mode": exp["mode"], "target_exposure": exp["target_exposure"], "cash": exp["cash"],
                     "kospi_above_40w": exp["kospi"]["above_40w"], "kosdaq_above_40w": exp["kosdaq"]["above_40w"],
                     "kospi_vol_percentile": exp["kospi"]["vol_percentile"], "kosdaq_vol_percentile": exp["kosdaq"]["vol_percentile"]},
        "sizing": {"max_positions": cfg["sizing"]["max_positions"], "base_slot_weight": cfg["sizing"]["base_slot_weight"]},
        "backtest": BACKTEST, "calendar": cal, "hold_days": cfg["holding"]["max_holding_days"],
        "initial_capital": cfg["portfolio"]["initial_capital"], "stocks": stocks, "buy_order": buy_order,
        "legacy_sell": legacy_sell, "tda_buy": tda_buy, "tda_sell": tda_sell,
        "tda_meta": {"has_ripser": bool(HAS_RIPSER), "window": tcfg.get("window", 60),
                     "embed_dim": tcfg.get("embed_dim", 3), "n": len(tda_df)},
        "all_stocks": fetch_all_stocks(auth, asof_str), "broad": broad, "overnight": overnight, "dart": dart_flags,
    }
    tpl = open(TEMPLATE, encoding="utf-8").read()
    html = tpl.replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    open(args.out, "w", encoding="utf-8").write(html)
    kb = os.path.getsize(args.out) / 1024
    print(f"생성: {args.out} ({kb:.0f}KB) | asof {asof.date()} | {exp['mode']} {exp['target_exposure']*100:.0f}% | "
          f"유니버스 {len(uni)} | 모멘텀 매수 {len(buy_order)}/이탈 {len(legacy_sell)} | "
          f"TDA 매수 {len(tda_buy)}/매도 {len(tda_sell)} (ripser={HAS_RIPSER}) | 종목 {len(stocks)}", file=sys.stderr)


if __name__ == "__main__":
    main()
