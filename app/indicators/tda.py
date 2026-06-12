"""위상수학적 데이터 분석(TDA) 기반 종목 발굴 — 연구개발계획서 방법론 구현.

방법론 (Gidea & Katz 2018, *Topological Data Analysis of Financial Time Series:
Landscapes of Crashes*, Physica A 491; arXiv:1703.04385 및 후속 연구):
  종목별 일별 로그수익률을 Takens 시간지연 임베딩 → 점구름 → Vietoris-Rips 지속성(H1) →
  지속성 풍경(persistence landscape)의 L2 노름 + 지속성 엔트로피 + 연속 다이어그램 간
  Wasserstein 거리(위상 변화 = 격동).

신호 부호 (5편 이상에서 반복 검증된 결과):
  지속성 노름이 '높거나 상승'하면 위상 불안정 = 위기/드로다운 전조 → **매도/회피**.
  낮고 안정적이면 평온 → **매수 적격**. TDA는 수익률의 '방향'이 아니라 '리스크'를 측정하므로
  방향은 고전 모멘텀/추세로 보강한다(Rudkin 2023; turbulence-index arXiv:2203.05603;
  null-validation arXiv:2602.00383).

검증 주의: 학술상 보호전략 Sharpe ~0.7-0.85 (방향 예측 아님, 드로다운 축소가 주효).
  개별종목 적용은 지수 대비 검증이 약하므로(짧은 윈도·적은 루프) 매끈한 특징
  (풍경노름·엔트로피·Wasserstein)만 사용하고 원시 H1 루프 '개수'는 신뢰하지 않는다.

구현: ripser+persim (H1). 미설치 시 scipy 단일연결(single-linkage) H0로 자동 강등(degraded)
  — H0 노름/엔트로피는 군집/분산 기술자로 H1보다 약하지만 배치가 죽지 않게 한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

try:
    from ripser import ripser
    from persim import PersLandscapeApprox, wasserstein
    from persim.persistent_entropy import persistent_entropy
    HAS_RIPSER = True
except Exception:  # pragma: no cover - 폴백 경로
    HAS_RIPSER = False

from scipy.cluster.hierarchy import linkage  # noqa: E402
from scipy.spatial.distance import pdist  # noqa: E402


def takens_embedding(x, dim=3, delay=1):
    """1D 시계열 → 시간지연 임베딩 점구름 (n, dim)."""
    x = np.asarray(x, dtype=float)
    n = len(x) - (dim - 1) * delay
    if n <= 1:
        return np.empty((0, dim))
    return np.column_stack([x[i * delay:i * delay + n] for i in range(dim)])


def _finite(d):
    return d[np.isfinite(d).all(axis=1)] if d is not None and d.size else np.empty((0, 2))


def stock_topology(returns, dim=3, delay=1):
    """수익률 윈도 → (L2 풍경노름, 지속성엔트로피, H1 다이어그램). 룩어헤드 없음."""
    cloud = takens_embedding(returns, dim, delay)
    if len(cloud) < 3:
        return 0.0, 0.0, np.empty((0, 2))
    if HAS_RIPSER:
        dgms = ripser(cloud, maxdim=1)["dgms"]
        h1 = _finite(dgms[1])
        try:   # 거래 희박 종목은 퇴화 다이어그램 → 풍경 노름 계산 실패 가능 → 0 처리
            norm = float(PersLandscapeApprox(dgms=dgms, hom_deg=1, num_steps=200).p_norm(p=2)) if len(h1) else 0.0
        except Exception:
            norm = 0.0
        try:
            ent = float(persistent_entropy(dgms, normalize=True)[1]) if len(h1) > 1 else 0.0  # 1개면 log(1)=0 분모
        except Exception:
            ent = 0.0
        return norm, ent, h1
    # ---- H0 폴백 (scipy 단일연결 = VR H0 정확 일치) ----
    Z = linkage(pdist(cloud), method="single")
    deaths = np.sort(Z[:, 2])
    norm = float(np.sqrt(np.sum((deaths / 2.0) ** 2)))           # tent 높이=수명/2 의 L2
    L = deaths[deaths > 0]
    ent = float(-np.sum((L / L.sum()) * np.log(L / L.sum())) / np.log(len(L))) if len(L) > 1 else 0.0
    return norm, ent, np.column_stack([np.zeros(len(deaths)), deaths])


def _wasserstein(a, b):
    if not HAS_RIPSER or a is None or b is None or len(a) == 0 or len(b) == 0:
        return np.nan
    try:
        return float(wasserstein(_finite(a), _finite(b)))
    except Exception:
        return np.nan


def _z(s):
    """횡단면 z-score, 윈저화 ±3."""
    s = pd.to_numeric(s, errors="coerce").astype(float)
    mu, sd = s.mean(), s.std(ddof=0)
    return ((s - mu) / sd).clip(-3, 3) if sd and sd > 0 else s * 0.0


def compute_tda_signals(daily_ind, asof, cfg):
    """유니버스 종목별 TDA 특징 → 위상리스크(risk)·추세(dir)·종합점수(score).

    asof 종가까지만 사용(슬라이딩 윈도 설계상 룩어헤드 없음). 반환: ticker별 DataFrame.
    """
    p = (cfg or {}).get("tda", {})
    win = int(p.get("window", 60)); dim = int(p.get("embed_dim", 3)); delay = int(p.get("delay", 1))
    lag = int(p.get("change_lag", 5)); lam = float(p.get("risk_lambda", 0.7))
    need = win + lag + 2
    sub = daily_ind[daily_ind["date"] <= asof]
    rows = []
    for tk, g in sub.groupby("ticker"):
        g = g.sort_values("date")
        c = g["close"].to_numpy(dtype=float)
        if len(c) < need or (c <= 0).any():
            continue
        ret = np.diff(np.log(c))
        norm_t, ent_t, h1_t = stock_topology(ret[-win:], dim, delay)
        norm_p, _, h1_p = stock_topology(ret[-win - lag:-lag], dim, delay)
        last = g.iloc[-1]
        mom = last.get("mom_6m_1m"); ma50 = last.get("ma50")
        trend = (c[-1] / ma50 - 1) if ma50 and not pd.isna(ma50) else np.nan
        rows.append({
            "ticker": tk, "pl_norm": norm_t, "pers_entropy": ent_t,
            "turb": _wasserstein(h1_p, h1_t), "d_norm": norm_t - norm_p,
            "mom": float(mom) if mom is not None and not pd.isna(mom) else np.nan,
            "trend": trend,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["zPLn"] = _z(df["pl_norm"]); df["zEnt"] = _z(df["pers_entropy"])
    df["zTurb"] = _z(df["turb"].fillna(df["turb"].median()))
    df["zDnorm"] = _z(df["d_norm"]); df["zMom"] = _z(df["mom"].fillna(df["mom"].median()))
    df["zTrend"] = _z(df["trend"].fillna(df["trend"].median()))
    df["instab"] = 0.5 * df["zPLn"] + 0.25 * df["zEnt"] + 0.25 * df["zTurb"]
    df["risk"] = 0.7 * df["instab"] + 0.3 * df["zDnorm"]      # 노름 '상승'에 가중(Gidea-Katz 전조)
    df["dir"] = 0.6 * df["zMom"] + 0.4 * df["zTrend"]
    df["score"] = df["dir"] - lam * df["risk"]
    df["risk_pct"] = df["risk"].rank(pct=True)                # 상위=위험
    return df


def tda_buy_sell(df, n_buy=8, n_sell=8):
    """매수 = 추세>0 & 위상리스크<중앙값, score 상위. 매도 = 위상리스크 상위25%, 위험-방향 상위."""
    if df is None or df.empty:
        return [], []
    risk_med = df["risk"].median()
    buy = df[(df["dir"] > 0) & (df["risk"] < risk_med)].sort_values("score", ascending=False)
    q75 = df["risk"].quantile(0.75)
    sell = df.assign(sell_score=df["risk"] - 0.5 * df["dir"])
    sell = sell[sell["risk"] >= q75].sort_values("sell_score", ascending=False)
    return list(buy.head(n_buy)["ticker"]), list(sell.head(n_sell)["ticker"])
