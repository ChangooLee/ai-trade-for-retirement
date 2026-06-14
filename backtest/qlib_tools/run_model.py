"""qlib 모델 실행 — 모델 교체 가능(LightGBM/LSTM/GRU/Transformer). 예측점수 저장 + IC + 절대 백테스트.

(a) ML 신호를 우리 리스크 하버스에 쓰려면 예측점수를 parquet로 내보내야 함 → /tmp/qlib_pred_<model>.parquet (date,ticker,score)
(b) 모델 비교(IC·수익·MDD).
GBDT=Alpha158(횡단면) · PyTorch(LSTM/GRU/Transformer)=Alpha360(시퀀스 d_feat=6) + TSDatasetH.
실행: MLFLOW_ALLOW_FILE_STORE=true MLFLOW_TRACKING_URI=file:///tmp/qlib_mlruns \
      .venv-qlib/bin/python -m backtest.qlib_tools.run_model --model lgbm
"""
from __future__ import annotations
import argparse, sys
import numpy as np, pandas as pd
import qlib
from qlib.constant import REG_US
from qlib.utils import init_instance_by_config
from qlib.workflow import R
from qlib.workflow.record_temp import SignalRecord, SigAnaRecord, PortAnaRecord

PROVIDER = "/tmp/qlib_krx"
TRAIN = ("2016-01-01", "2020-12-31"); VALID = ("2021-01-01", "2021-12-31"); TEST = ("2022-01-01", "2026-06-05")
PYTORCH = {"lstm", "gru", "alstm", "transformer"}

MODELS = {
    "lgbm": {"class": "LGBModel", "module_path": "qlib.contrib.model.gbdt",
             "kwargs": {"loss": "mse", "learning_rate": 0.03, "num_leaves": 31, "n_estimators": 300,
                        "subsample": 0.8, "colsample_bytree": 0.8, "min_child_samples": 50, "num_threads": 4}},
    "lstm": {"class": "LSTM", "module_path": "qlib.contrib.model.pytorch_lstm",
             "kwargs": {"d_feat": 6, "hidden_size": 64, "num_layers": 2, "dropout": 0.0, "n_epochs": 50,
                        "lr": 1e-3, "early_stop": 15, "batch_size": 800, "metric": "loss", "loss": "mse", "GPU": -1}},
    "gru": {"class": "GRU", "module_path": "qlib.contrib.model.pytorch_gru",
            "kwargs": {"d_feat": 6, "hidden_size": 64, "num_layers": 2, "dropout": 0.0, "n_epochs": 50,
                       "lr": 1e-3, "early_stop": 15, "batch_size": 800, "metric": "loss", "loss": "mse", "GPU": -1}},
    "transformer": {"class": "TransformerModel", "module_path": "qlib.contrib.model.pytorch_transformer",
                    "kwargs": {"d_feat": 6, "d_model": 64, "nhead": 4, "num_layers": 2, "dropout": 0.1, "n_epochs": 40,
                               "lr": 1e-4, "early_stop": 15, "batch_size": 800, "metric": "loss", "loss": "mse", "GPU": -1}},
}


def build_dataset(model_name, instruments="all"):
    handler_cls = "Alpha360" if model_name in PYTORCH else "Alpha158"
    handler = {"class": handler_cls, "module_path": "qlib.contrib.data.handler",
               "kwargs": {"start_time": TRAIN[0], "end_time": TEST[1], "fit_start_time": TRAIN[0],
                          "fit_end_time": TRAIN[1], "instruments": instruments}}
    segs = {"train": TRAIN, "valid": VALID, "test": TEST}
    # non-ts 모델은 Alpha360(360=60일×6) flat을 모델이 내부에서 (60,6)으로 reshape → DatasetH 사용(공식 벤치마크 방식)
    return init_instance_by_config({"class": "DatasetH", "module_path": "qlib.data.dataset",
                                    "kwargs": {"handler": handler, "segments": segs}})


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--model", default="lgbm", choices=list(MODELS))
    ap.add_argument("--instruments", default="all")          # all | liquid(유동성 상위, 메모리 절감)
    ap.add_argument("--gpu", type=int, default=-1)           # -1=CPU, 0=GPU0 (서버 RTX3090)
    ap.add_argument("--kernels", type=int, default=1)        # 피처 로딩 프로세스 수(macOS=1 권장, Linux는 늘려도 됨)
    a = ap.parse_args()
    qlib.init(provider_uri=PROVIDER, region=REG_US, kernels=a.kernels)
    dataset = build_dataset(a.model, a.instruments)
    cfg = MODELS[a.model]
    if a.model in PYTORCH:
        cfg["kwargs"]["GPU"] = a.gpu
    model = init_instance_by_config(cfg)
    port_cfg = {
        "executor": {"class": "SimulatorExecutor", "module_path": "qlib.backtest.executor",
                     "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True}},
        "strategy": {"class": "TopkDropoutStrategy", "module_path": "qlib.contrib.strategy",
                     "kwargs": {"signal": (model, dataset), "topk": 30, "n_drop": 3}},
        "backtest": {"start_time": TEST[0], "end_time": TEST[1], "account": 100000000, "benchmark": "005930",
                     "exchange_kwargs": {"freq": "day", "limit_threshold": None, "deal_price": "close",
                                         "open_cost": 0.00175, "close_cost": 0.00175, "min_cost": 0}}}
    with R.start(experiment_name=f"krx_{a.model}"):
        model.fit(dataset)
        rec = R.get_recorder()
        # 예측점수 저장 (우리 하버스가 신호로 사용)
        pred = model.predict(dataset)
        pred = pred.to_frame("score") if isinstance(pred, pd.Series) else pred
        pred.index = pred.index.set_names(["date", "ticker"])
        out = f"/tmp/qlib_pred_{a.model}.parquet"
        pred.reset_index().to_parquet(out)
        print(f"\n예측점수 저장 → {out} ({len(pred)}행)", file=sys.stderr)
        SignalRecord(model, dataset, rec).generate()
        SigAnaRecord(rec).generate()
        PortAnaRecord(rec, port_cfg, "day").generate()
        try:
            ic = rec.load_object("sig_analysis/ic.pkl")
            print(f"\n[{a.model}] IC={ic.mean():.4f} ICIR={ic.mean()/ic.std():.3f}")
        except Exception as e:
            print("ic:", e)
        try:
            rep = rec.load_object("portfolio_analysis/report_normal_1day.pkl")
            r = rep["return"].fillna(0); eq = (1 + r).cumprod()
            print(f"[{a.model}] 절대(top30, OOS {TEST[0]}~{TEST[1]}): 총수익 {eq.iloc[-1]-1:+.1%} · "
                  f"MDD {float((eq/eq.cummax()-1).min()):+.1%} · Sharpe {r.mean()/r.std()*np.sqrt(252):.2f}")
        except Exception as e:
            print("report:", e)


if __name__ == "__main__":
    main()
