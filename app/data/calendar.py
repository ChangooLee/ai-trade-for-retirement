"""거래일 캘린더 유틸 — 룩어헤드 방지(다음 거래일 시가 집행)와 보유일 계산용."""
from __future__ import annotations

import pandas as pd


def trading_calendar(daily: pd.DataFrame) -> list:
    return sorted(pd.to_datetime(daily["date"]).unique())


def resolve_asof(asof_arg, daily: pd.DataFrame):
    """asof 미지정 시 최신 거래일. 지정 시 휴장일이면 직전 거래일로 보정."""
    cal = trading_calendar(daily)
    if not asof_arg:
        return pd.Timestamp(cal[-1]), False
    want = pd.Timestamp(str(asof_arg))
    le = [d for d in cal if d <= want]
    if not le:
        return pd.Timestamp(cal[0]), True
    resolved = pd.Timestamp(le[-1])
    corrected = resolved != want
    return resolved, corrected


def last_completed_week_cutoff(asof) -> pd.Timestamp:
    """주봉 컷오프 — 주봉 신호는 주 마감(금) 후에만 확정(§20.2 룩어헤드 방지·검증 일관성).

    asof가 주중(월~목)이면 진행 중인 미완성 주를 제외하고 직전 완성주까지만 쓰도록
    직전 일요일을 돌려준다. 금/토/일이면 해당 주가 완성된 것으로 보고 asof 그대로.
    이렇게 하면 주봉 눌림 신호가 주중 2~3일치 부분 봉으로 매일 흔들리지 않고
    매주 마감에 한 번 갱신된다.
    """
    asof = pd.Timestamp(asof)
    if asof.weekday() >= 4:        # 금(4)·토(5)·일(6): 해당 주 완성
        return asof
    return asof - pd.Timedelta(days=asof.weekday() + 1)   # 직전 일요일(현재 주 제외)


def next_trading_day(cal: list, asof: pd.Timestamp) -> pd.Timestamp:
    """집행 가정일 = asof 다음 거래일. 데이터 끝이면 다음 영업일로 추정."""
    after = [d for d in cal if d > asof]
    if after:
        return pd.Timestamp(after[0])
    nb = pd.Timestamp(asof) + pd.offsets.BDay(1)
    return nb


def trading_days_between(cal: list, start, end) -> int:
    """(start, end] 사이 거래일 수 (보유일 계산)."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    return sum(1 for d in cal if s < d <= e)


def nth_trading_day_from(cal: list, start, n: int):
    """start(포함) 기준 n거래일 뒤 날짜. 데이터 범위를 넘으면 영업일 추정."""
    s = pd.Timestamp(start)
    fut = [d for d in cal if d >= s]
    if len(fut) > n:
        return pd.Timestamp(fut[n])
    extra = n - (len(fut) - 1)
    return pd.Timestamp(fut[-1]) + pd.offsets.BDay(extra) if fut else s + pd.offsets.BDay(n)
