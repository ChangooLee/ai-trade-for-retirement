# qlib 리서치 샌드박스 (KRX)

Microsoft [qlib](https://github.com/microsoft/qlib)을 **별도 리서치 환경**으로 붙여, 우리 KRX PIT 데이터에
qlib의 ML 모델·Alpha158/360 팩터·백테스트 리포트를 쓸 수 있게 한다. **프로덕션(시뮬·웹앱·일배치)과 분리** —
qlib은 "ML이 우리 룰을 이기나" 검증 + 팩터 리서치용이며, 라이브 매매 경로에 들어가지 않는다.

## 왜 분리 환경인가
- qlib은 Python ≤3.12 지원(우리 메인 venv는 3.13) · pandas 2.x 호환 이슈 → **별도 py3.11 venv(`.venv-qlib`)**.
- KRX는 qlib 네이티브 미지원 → 우리 PIT 패널을 qlib 바이너리로 **변환** 필요.

## 1회 설정
```bash
# 1) py3.11 qlib 환경
python3.11 -m venv .venv-qlib
.venv-qlib/bin/pip install pyqlib fire

# 2) dump_bin.py(qlib 변환 도구) 내려받기 — 3rd-party(MIT), 저장소엔 미포함
curl -sL https://raw.githubusercontent.com/microsoft/qlib/main/scripts/dump_bin.py \
  -o backtest/qlib_tools/dump_bin.py

# 3) LightGBM용 OpenMP(맥)
brew install libomp
```

## 데이터 변환 + 워크플로
```bash
# KRX PIT(수정주가) → qlib CSV (메인 venv: PIT 로더 사용)
.venv/bin/python -m backtest.qlib_tools.convert_krx          # → /tmp/qlib_krx_csv/<종목>.csv

# CSV → qlib 바이너리
.venv-qlib/bin/python -c "import sys; sys.path.insert(0,'backtest/qlib_tools'); \
  from dump_bin import DumpDataAll; \
  DumpDataAll('/tmp/qlib_krx_csv','/tmp/qlib_krx', include_fields='open,high,low,close,volume,factor,money', date_field_name='date').dump()"

# Alpha158 + LightGBM + top-k 백테스트 (OOS 2022~2026)
MLFLOW_ALLOW_FILE_STORE=true MLFLOW_TRACKING_URI=file:///tmp/qlib_mlruns \
  .venv-qlib/bin/python -m backtest.qlib_tools.krx_workflow
```

## 산출물 / 주의
- 변환 데이터(`/tmp/qlib_krx*`)·`.venv-qlib`·`dump_bin.py`·mlruns는 **gitignore**(재현 가능, 무거움).
- factor=1.0 으로 저장: 우리 PIT는 이미 분할/감자 역보정된 수정주가라 qlib이 그대로 조정가로 취급.
- 결론(우리 검증, OOS 2022~2026 · top30): qlib Alpha158+LGBM은 **예측 IC 0.048(양+)·raw 수익 +206%로 우리 룰(+167%)보다 높았으나 MDD −47%로 2배+ 깊음**(우리 룰 −21%). 위험조정은 비슷, 손실회피 기준엔 우리 룰 우위. 전체기간(2017~2026)·단순 ML은 우리 룰에 패배(Part B). **→ 프로덕션은 룰 유지, qlib은 리서치용 보존**(Alpha158 신호+강한 리스크관리 결합은 향후 과제).
