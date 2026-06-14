"""qlib 네이티브 워크플로 — KRX(우리 PIT) 데이터에 Alpha158 + LightGBM + top-k 백테스트.

목적: Part B(우리 하버스 ML 벤치마크)를 qlib 프레임워크로 교차검증 + qlib의 IC/리스크 리포트 활용.
데이터: /tmp/qlib_krx (convert_krx + dump_bin으로 생성). 이미 수정주가(factor=1).
분할: train 2016~2020 / valid 2021 / test 2022~2026(OOS).
실행: .venv-qlib/bin/python -m backtest.qlib_tools.krx_workflow   (py3.11, qlib 설치 env)
"""
from __future__ import annotations
import sys
import qlib
from qlib.constant import REG_US
from qlib.utils import init_instance_by_config
from qlib.workflow import R
from qlib.workflow.record_temp import SignalRecord, SigAnaRecord, PortAnaRecord

PROVIDER = "/tmp/qlib_krx"
TRAIN = ("2016-01-01", "2020-12-31"); VALID = ("2021-01-01", "2021-12-31"); TEST = ("2022-01-01", "2026-06-05")


def main():
    qlib.init(provider_uri=PROVIDER, region=REG_US)
    handler = {"class": "Alpha158", "module_path": "qlib.contrib.data.handler",
               "kwargs": {"start_time": TRAIN[0], "end_time": TEST[1],
                          "fit_start_time": TRAIN[0], "fit_end_time": TRAIN[1], "instruments": "all"}}
    dataset = init_instance_by_config({
        "class": "DatasetH", "module_path": "qlib.data.dataset",
        "kwargs": {"handler": handler, "segments": {"train": TRAIN, "valid": VALID, "test": TEST}}})
    model = init_instance_by_config({
        "class": "LGBModel", "module_path": "qlib.contrib.model.gbdt",
        "kwargs": {"loss": "mse", "learning_rate": 0.03, "num_leaves": 31, "n_estimators": 300,
                   "subsample": 0.8, "colsample_bytree": 0.8, "min_child_samples": 50, "num_threads": 4}})
    port_cfg = {
        "executor": {"class": "SimulatorExecutor", "module_path": "qlib.backtest.executor",
                     "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True}},
        "strategy": {"class": "TopkDropoutStrategy", "module_path": "qlib.contrib.strategy",
                     "kwargs": {"signal": (model, dataset), "topk": 30, "n_drop": 3}},
        "backtest": {"start_time": TEST[0], "end_time": TEST[1], "account": 100000000, "benchmark": "005930",
                     "exchange_kwargs": {"freq": "day", "limit_threshold": None, "deal_price": "close",
                                         "open_cost": 0.00175, "close_cost": 0.00175, "min_cost": 0}}}
    with R.start(experiment_name="krx_alpha158_lgbm"):
        model.fit(dataset)
        R.save_objects(**{"params.pkl": model})
        rec = R.get_recorder()
        SignalRecord(model, dataset, rec).generate()
        SigAnaRecord(rec).generate()                              # IC·ICIR·Rank IC
        PortAnaRecord(rec, port_cfg, "day").generate()            # 수익·MDD·IR
        print("\n=== qlib 신호 분석(IC) ===", file=sys.stderr)
        try:
            print(rec.load_object("sig_analysis/ic.pkl").describe(), file=sys.stderr)
        except Exception as e:
            print("ic load:", e, file=sys.stderr)
        print("\n=== qlib 포트폴리오 분석(test OOS 2022~2026, top30, 벤치=삼성전자) ===")
        try:
            pa = rec.load_object("portfolio_analysis/port_analysis_1day.pkl")
            print(pa.to_string())
        except Exception as e:
            print("port_analysis load:", e)
        # 절대 수익·MDD (벤치 무관) — report_normal의 일별 수익에서 직접 계산
        try:
            import numpy as np
            rep = rec.load_object("portfolio_analysis/report_normal_1day.pkl")
            r = rep["return"].fillna(0)                          # 비용 차감 전략 일별수익
            eq = (1 + r).cumprod()
            total = eq.iloc[-1] - 1
            mdd = float((eq / eq.cummax() - 1).min())
            shp = r.mean() / r.std() * np.sqrt(252) if r.std() else 0
            print(f"\n[절대] qlib ML top30 (2022~2026 OOS): 총수익 {total:+.1%} · MDD {mdd:+.1%} · Sharpe {shp:.2f} · 거래일 {len(r)}")
        except Exception as e:
            print("report_normal load:", e)


if __name__ == "__main__":
    main()
