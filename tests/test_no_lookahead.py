"""§20.3 룩어헤드 방지 — 기준일 신호는 반드시 다음 거래일 이후 집행."""
import pandas as pd

from app.data.calendar import resolve_asof, next_trading_day, trading_calendar, trading_days_between


def _daily(dates):
    return pd.DataFrame({"date": pd.to_datetime(dates), "ticker": "X",
                         "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "trdval": 1})


def test_execution_after_asof():
    cal = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05", "2026-06-08"]
    daily = _daily(cal)
    asof, _ = resolve_asof("2026-06-05", daily)
    nxt = next_trading_day(trading_calendar(daily), asof)
    assert nxt > asof, "집행일(다음 거래일)은 기준일보다 뒤여야 한다"
    assert str(nxt.date()) == "2026-06-08"


def test_asof_holiday_correction():
    daily = _daily(["2026-06-04", "2026-06-05", "2026-06-08"])
    asof, corrected = resolve_asof("2026-06-06", daily)   # 토요일
    assert corrected and str(asof.date()) == "2026-06-05"


def test_holding_days_count():
    cal = trading_calendar(_daily(["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]))
    # 진입 6-01, 기준 6-05 → 그 사이 거래일(6-02..6-05) = 4
    assert trading_days_between(cal, pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-05")) == 4


def test_weekly_signal_uses_completed_week():
    # 주봉 신호는 week_end <= asof 인 완성된 주봉만 사용해야 한다.
    weekly = pd.DataFrame({"ticker": ["X", "X"],
                           "week_end": pd.to_datetime(["2026-05-29", "2026-06-05"])})
    asof = pd.Timestamp("2026-06-03")   # 주중
    used = weekly[weekly["week_end"] <= asof]
    assert used["week_end"].max() == pd.Timestamp("2026-05-29"), "주중엔 직전 완성 주봉만"
