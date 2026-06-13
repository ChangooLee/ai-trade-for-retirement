"""활성 알고리즘 시뮬레이션 일별 전진 — 매일 배치(build_webapp 다음)에서 실행.

state/daily_signals.json(그날 시그널·가격) + state/sim.db(사용자별 시뮬 상태)를 읽어,
로그인 후 시뮬을 시작한 각 사용자의 가상 포트폴리오를 하루치 집행(매수/매도/평가)하고 손익을 누적한다.
멱등: 같은 거래일은 한 번만 처리(last_processed 비교). 미시작/미래 시작은 건너뜀.

사용: python -m app.batch.run_sims
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, ".")
from app.sim import db, engine  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIG_PATH = os.path.join(_REPO, "state", "daily_signals.json")


def main():
    if not os.path.exists(SIG_PATH):
        print("시그널 파일 없음 — build_webapp 먼저 실행 필요. 건너뜀.", file=sys.stderr); return
    try:
        sig = json.load(open(SIG_PATH, encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"시그널 파일 손상/읽기 실패 — 건너뜀(다음 빌드에 복구): {e}", file=sys.stderr); return
    asof = sig.get("asof")
    if not asof:
        print("시그널에 asof 없음 — 건너뜀.", file=sys.stderr); return
    db.init()
    actives = db.list_active()
    done = skipped = 0
    for s in actives:
        if s["start_date"] and s["start_date"] > asof:        # 아직 시작 전(미래)
            skipped += 1; continue
        if s["last_processed"] and s["last_processed"] >= asof:  # 이미 이 거래일 처리됨(멱등)
            skipped += 1; continue
        try:
            state = db.state_from_row(s)
            ns, r = engine.execute_day(state, asof, sig)
            db.save_step(s["sub"], ns, r)
            done += 1
        except Exception as e:
            print(f"  ! {s['sub']} 처리 실패: {e}", file=sys.stderr)
    print(f"시뮬 전진 완료: 처리 {done} · 건너뜀 {skipped} · 활성 {len(actives)} (asof {asof})", file=sys.stderr)


if __name__ == "__main__":
    main()
