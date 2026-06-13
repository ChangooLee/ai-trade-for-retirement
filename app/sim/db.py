"""시뮬레이터 영속 — SQLite(stdlib, 무의존성). 단일 서버·저동시성에 최적, WAL로 배치·API 동시접근 안전.

테이블:
  sim    (sub PK) 사용자별 시뮬 상태: 투자금·시작일·상태·현금·서킷브레이커 앵커·현재 포지션(JSON)·마지막 처리일
  trade  청산 체결 이력(사용자가 보는 매도 기록)
  equity 일별 에쿼티 곡선
포지션은 작업 상태라 sim.positions_json에 보관, 체결·에쿼티는 조회용 테이블로 분리.
"""
from __future__ import annotations
import json, os, sqlite3, datetime as dt

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "state", "sim.db")


def _conn(path=None):
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init(path=None):
    c = _conn(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sim(
      sub TEXT PRIMARY KEY, email TEXT, investment REAL NOT NULL, start_date TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active', created_at TEXT, last_processed TEXT,
      cash REAL, cb_month TEXT, cb_base_pnl REAL, positions_json TEXT DEFAULT '[]',
      cb_limit REAL DEFAULT 0.03, exposure_mult REAL DEFAULT 1.0);
    CREATE TABLE IF NOT EXISTS trade(
      id INTEGER PRIMARY KEY AUTOINCREMENT, sub TEXT, ticker TEXT, name TEXT,
      entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL, shares INTEGER,
      pnl REAL, ret REAL, days INTEGER, reason TEXT);
    CREATE INDEX IF NOT EXISTS ix_trade_sub ON trade(sub);
    CREATE TABLE IF NOT EXISTS equity(
      sub TEXT, date TEXT, equity REAL, cash REAL, holdings_value REAL,
      n_positions INTEGER, tripped INTEGER, PRIMARY KEY(sub, date));
    """)
    for col, dflt in (("cb_limit", "0.03"), ("exposure_mult", "1.0")):   # 기존 DB 마이그레이션
        try:
            c.execute(f"ALTER TABLE sim ADD COLUMN {col} REAL DEFAULT {dflt}")
        except sqlite3.OperationalError:
            pass   # 이미 있음
    c.commit(); c.close()


def get_sim(sub, path=None):
    c = _conn(path)
    r = c.execute("SELECT * FROM sim WHERE sub=?", (sub,)).fetchone()
    c.close()
    return dict(r) if r else None


def start_sim(sub, email, investment, start_date, path=None, cb_limit=0.03, exposure_mult=1.0):
    """시뮬 시작/리셋 — 기존 기록(체결·에쿼티)을 지우고 그날부터 새로 시작. cb_limit·exposure_mult 조정 가능."""
    c = _conn(path)
    now = dt.datetime.utcnow().isoformat()
    c.execute("DELETE FROM trade WHERE sub=?", (sub,))
    c.execute("DELETE FROM equity WHERE sub=?", (sub,))
    c.execute("""INSERT INTO sim(sub,email,investment,start_date,status,created_at,last_processed,cash,cb_month,cb_base_pnl,positions_json,cb_limit,exposure_mult)
                 VALUES(?,?,?,?,'active',?,NULL,?,NULL,0,'[]',?,?)
                 ON CONFLICT(sub) DO UPDATE SET email=excluded.email, investment=excluded.investment,
                   start_date=excluded.start_date, status='active', created_at=excluded.created_at,
                   last_processed=NULL, cash=excluded.cash, cb_month=NULL, cb_base_pnl=0, positions_json='[]',
                   cb_limit=excluded.cb_limit, exposure_mult=excluded.exposure_mult""",
              (sub, email, float(investment), start_date, now, float(investment), float(cb_limit), float(exposure_mult)))
    c.commit(); c.close()


def stop_sim(sub, path=None):
    c = _conn(path); c.execute("UPDATE sim SET status='stopped' WHERE sub=?", (sub,)); c.commit(); c.close()


def state_from_row(row):
    return {"investment": row["investment"], "cash": row["cash"] if row["cash"] is not None else row["investment"],
            "positions": json.loads(row["positions_json"] or "[]"),
            "cb_month": row["cb_month"], "cb_base_pnl": row["cb_base_pnl"] or 0.0,
            "cb_limit": (row["cb_limit"] if row["cb_limit"] is not None else 0.03)}


def save_step(sub, new_state, result, path=None):
    """하루치 결과 저장: 상태 갱신 + 체결 추가 + 에쿼티 upsert + last_processed 갱신."""
    c = _conn(path)
    c.execute("""UPDATE sim SET cash=?, cb_month=?, cb_base_pnl=?, positions_json=?, last_processed=? WHERE sub=?""",
              (new_state["cash"], new_state["cb_month"], new_state["cb_base_pnl"],
               json.dumps(new_state["positions"], ensure_ascii=False), result["date"], sub))
    for t in result["trades"]:
        c.execute("""INSERT INTO trade(sub,ticker,name,entry_date,entry_price,exit_date,exit_price,shares,pnl,ret,days,reason)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (sub, t["ticker"], t["name"], t["entry_date"], t["entry_price"], t["exit_date"],
                   t["exit_price"], t["shares"], t["pnl"], t["ret"], t["days"], t["reason"]))
    c.execute("""INSERT INTO equity(sub,date,equity,cash,holdings_value,n_positions,tripped) VALUES(?,?,?,?,?,?,?)
                 ON CONFLICT(sub,date) DO UPDATE SET equity=excluded.equity, cash=excluded.cash,
                   holdings_value=excluded.holdings_value, n_positions=excluded.n_positions, tripped=excluded.tripped""",
              (sub, result["date"], result["equity"], result["cash"], result["holdings_value"],
               result["n_positions"], 1 if result["tripped"] else 0))
    c.commit(); c.close()


def list_active(path=None):
    c = _conn(path)
    rows = c.execute("SELECT * FROM sim WHERE status='active'").fetchall()
    c.close()
    return [dict(r) for r in rows]


def results(sub, path=None):
    """프런트 표시용: 상태 요약 + 체결 + 에쿼티 곡선."""
    c = _conn(path)
    s = c.execute("SELECT * FROM sim WHERE sub=?", (sub,)).fetchone()
    if not s:
        c.close(); return None
    s = dict(s)
    trades = [dict(r) for r in c.execute("SELECT * FROM trade WHERE sub=? ORDER BY exit_date DESC, id DESC", (sub,)).fetchall()]
    eq = [dict(r) for r in c.execute("SELECT date,equity FROM equity WHERE sub=? ORDER BY date", (sub,)).fetchall()]
    c.close()
    inv = s["investment"]
    last_eq = eq[-1]["equity"] if eq else inv
    realized = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    positions = json.loads(s["positions_json"] or "[]")
    return {
        "active": s["status"] == "active", "investment": inv, "start_date": s["start_date"],
        "cb_limit": s["cb_limit"] if s["cb_limit"] is not None else 0.03,
        "exposure_mult": s["exposure_mult"] if s["exposure_mult"] is not None else 1.0,
        "last_processed": s["last_processed"], "equity": last_eq, "cash": s["cash"],
        "total_pnl": last_eq - inv, "total_ret": (last_eq / inv - 1) if inv else 0.0,
        "realized_pnl": realized, "n_trades": len(trades),
        "win_rate": (wins / len(trades)) if trades else 0.0, "n_positions": len(positions),
        "positions": positions, "trades": trades, "equity_curve": eq,
    }
