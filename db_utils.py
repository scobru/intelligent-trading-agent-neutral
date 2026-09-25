"""
Persistenza SQLite: snapshot del portafoglio, osservazioni del funding,
operazioni, errori. Stesso approccio dei bot fratelli: un file, nessun
server. Lo storico del funding non e' solo per la dashboard: la decisione
di entrare si basa sulla sua media.
"""

import json
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    mode TEXT,
    total_value_usd REAL,
    idle_usd REAL,
    invested_usd REAL,
    hedged_notional_usd REAL,
    weighted_apr REAL,
    positions_json TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS funding_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    asset TEXT NOT NULL,
    symbol TEXT,
    spot_price REAL,
    mark_price REAL,
    fair_price REAL,
    total_long REAL,
    total_short REAL,
    premium_pct REAL,
    short_apr REAL
);
CREATE INDEX IF NOT EXISTS idx_funding_asset_time ON funding_observations(asset, created_at);

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    operation TEXT,
    asset TEXT,
    symbol TEXT,
    amount_usd REAL,
    apr REAL,
    status TEXT,
    llm_reason TEXT,
    result_reason TEXT,
    trigger TEXT,
    decision_json TEXT,
    result_json TEXT,
    system_prompt TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT,
    error_type TEXT,
    message TEXT,
    traceback TEXT,
    context_json TEXT
);

CREATE TABLE IF NOT EXISTS bot_control (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.SQLITE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str = None) -> None:
    with get_connection(path) as conn:
        conn.executescript(SCHEMA)


def _json(val: Any) -> str:
    try:
        return json.dumps(val, default=str)
    except Exception:
        return json.dumps(str(val))


def log_snapshot(status: Dict[str, Any], path: str = None) -> int:
    init_db(path)
    with get_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO snapshots (created_at, mode, total_value_usd, idle_usd, invested_usd,
               hedged_notional_usd, weighted_apr, positions_json, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), status.get("mode"), status.get("total_value_usd"), status.get("idle_usd"),
             status.get("invested_usd"), status.get("hedged_notional_usd"), status.get("weighted_apr"),
             _json(status.get("positions", [])), _json(status)),
        )
        return cur.lastrowid


def log_funding(markets: Dict[str, Dict[str, Any]], path: str = None) -> int:
    init_db(path)
    now = _now()
    rows = [(now, m["asset"], m["symbol"], m["spot_price"], m["mark_price"], m["fair_price"],
             m["total_long"], m["total_short"], m["premium_pct"], m["short_apr"])
            for m in markets.values()]
    with get_connection(path) as conn:
        conn.executemany(
            """INSERT INTO funding_observations (created_at, asset, symbol, spot_price, mark_price,
               fair_price, total_long, total_short, premium_pct, short_apr)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    return len(rows)


def funding_history(hours: float, path: str = None) -> Dict[str, List[float]]:
    """APR dello short osservati nelle ultime `hours` ore, per asset."""
    init_db(path)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    out: Dict[str, List[float]] = {}
    with get_connection(path) as conn:
        for r in conn.execute(
            "SELECT asset, short_apr FROM funding_observations WHERE created_at >= ? ORDER BY id",
            (since,),
        ):
            if r["short_apr"] is not None:
                out.setdefault(r["asset"], []).append(float(r["short_apr"]))
    return out


def log_operation(decision: Dict[str, Any], result: Dict[str, Any],
                  system_prompt: str = None, path: str = None) -> int:
    init_db(path)
    decision = decision or {}
    result = result or {}
    with get_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO operations (created_at, operation, asset, symbol, amount_usd, apr, status,
               llm_reason, result_reason, trigger, decision_json, result_json, system_prompt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), result.get("operation") or decision.get("operation"),
             result.get("asset") or decision.get("asset"), result.get("symbol"),
             result.get("amount_usd"), result.get("apr"), result.get("status"), decision.get("reason"),
             result.get("reason") if result.get("status") in ("rejected", "error") else None,
             result.get("trigger"), _json(decision), _json(result), system_prompt),
        )
        return cur.lastrowid


def log_error(exc: BaseException, context: Optional[Dict[str, Any]] = None,
              source: str = "neutral_agent", path: str = None) -> int:
    init_db(path)
    with get_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO errors (created_at, source, error_type, message, traceback, context_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_now(), source, type(exc).__name__, str(exc),
             "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
             _json(context or {})),
        )
        return cur.lastrowid


def fetch_dashboard_data(path: str = None, limit: int = 50) -> Dict[str, Any]:
    """Tutto cio' che serve alla dashboard, in una lettura."""
    init_db(path)
    with get_connection(path) as conn:
        latest = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        equity = conn.execute(
            """SELECT created_at, total_value_usd, invested_usd, hedged_notional_usd FROM
               (SELECT * FROM snapshots ORDER BY id DESC LIMIT 500) ORDER BY id ASC"""
        ).fetchall()
        funding_series = conn.execute(
            """SELECT created_at, asset, short_apr FROM
               (SELECT * FROM funding_observations ORDER BY id DESC LIMIT 1000) ORDER BY id ASC"""
        ).fetchall()
        funding_latest = conn.execute(
            """SELECT * FROM funding_observations WHERE id IN
               (SELECT MAX(id) FROM funding_observations GROUP BY asset) ORDER BY asset"""
        ).fetchall()
        ops = conn.execute("SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        errors = conn.execute("SELECT id, created_at, source, error_type, message FROM errors "
                              "ORDER BY id DESC LIMIT 20").fetchall()

    status = json.loads(latest["raw_json"]) if latest else None
    return {
        "status": status,
        "snapshot_at": latest["created_at"] if latest else None,
        "equity": [dict(r) for r in equity],
        "funding_series": [dict(r) for r in funding_series],
        "funding_latest": [dict(r) for r in funding_latest],
        "operations": [
            {k: r[k] for k in r.keys() if k not in ("system_prompt", "decision_json")}
            for r in ops
        ],
        "errors": [dict(r) for r in errors],
    }


def is_bot_paused() -> bool:
    """Verifica se il bot neutral e' in stato di pausa."""
    try:
        init_db()
        with get_connection() as conn:
            row = conn.execute("SELECT value FROM bot_control WHERE key = 'is_paused'").fetchone()
            if row:
                return str(row["value"]).lower() in ("1", "true", "yes")
    except Exception:
        pass
    return False


def get_pause_info() -> Dict[str, Any]:
    """Recupera dettagli sullo stato di pausa del bot."""
    info = {"is_paused": False, "reason": "", "updated_at": ""}
    try:
        init_db()
        with get_connection() as conn:
            rows = conn.execute("SELECT key, value, updated_at FROM bot_control WHERE key IN ('is_paused', 'pause_reason')").fetchall()
            for row in rows:
                if row["key"] == "is_paused":
                    info["is_paused"] = str(row["value"]).lower() in ("1", "true", "yes")
                    info["updated_at"] = row["updated_at"]
                elif row["key"] == "pause_reason":
                    info["reason"] = row["value"]
    except Exception:
        pass
    return info


def set_bot_paused(paused: bool, reason: str = "") -> None:
    """Imposta o rimuove lo stato di pausa del bot neutral."""
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    val = "1" if paused else "0"
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO bot_control (key, value, updated_at) VALUES ('is_paused', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
        """, (val, now))
        conn.execute("""
            INSERT INTO bot_control (key, value, updated_at) VALUES ('pause_reason', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
        """, (reason or ("Pausa da Coordinatore/Operatore" if paused else "Operativo"), now))

