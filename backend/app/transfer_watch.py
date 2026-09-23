from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Any

from app.database import get_data_dir

INTERVAL_SECONDS = 30
_stop = threading.Event()
_scan_lock = threading.Lock()
_thread: threading.Thread | None = None


@contextmanager
def connect():
    path = get_data_dir() / "transfer-watch.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS routes (
            symbol TEXT NOT NULL, exchange TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            snapshot TEXT, baseline TEXT, PRIMARY KEY(symbol, exchange));
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, exchange TEXT NOT NULL,
            detail TEXT NOT NULL, created_at TEXT NOT NULL, pushed INTEGER NOT NULL DEFAULT 0);
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def normalize_routes(items):
    routes = set()
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError("最多监控 20 个币种")
    for item in items:
        symbol = str(item.get("symbol", "")).strip().upper()
        exchanges = item.get("selectedExchanges", [])
        if not re.fullmatch(r"[A-Z0-9_]{1,32}", symbol) or not isinstance(exchanges, list):
            raise ValueError("币种或交易所无效")
        for exchange in exchanges:
            if exchange not in {"bn", "by", "gt", "okx", "bg", "as"}:
                raise ValueError("交易所无效")
            routes.add((symbol, exchange))
    return routes


def sync(items):
    routes = normalize_routes(items)
    with _scan_lock, connect() as db:
        active = {(r["symbol"], r["exchange"]) for r in db.execute("SELECT * FROM routes WHERE enabled=1")}
        db.execute("UPDATE routes SET enabled=0")
        for symbol, exchange in routes:
            db.execute("INSERT OR IGNORE INTO routes(symbol,exchange) VALUES (?,?)", (symbol, exchange))
            db.execute("UPDATE routes SET enabled=1 WHERE symbol=? AND exchange=?", (symbol, exchange))
            if (symbol, exchange) not in active:
                db.execute("UPDATE routes SET baseline=NULL,snapshot=NULL WHERE symbol=? AND exchange=?", (symbol, exchange))
        # Stopped routes must not replay pending reminders after re-adding them.
        for symbol, exchange in active - routes:
            db.execute("UPDATE events SET pushed=2 WHERE symbol=? AND exchange=? AND pushed=0", (symbol, exchange))
    return overview()


def known_flags(snapshot):
    flags = {}
    for chain in snapshot.get("chains") or []:
        name = str(chain.get("chain") or "").strip()
        if not name:
            continue
        for field in ("depositEnabled", "withdrawEnabled"):
            value = chain.get(field)
            if isinstance(value, bool):
                flags[(name, field)] = value
    return flags


def changes(previous, current):
    if current.get("status") != "ok":
        return []
    old = known_flags(previous)
    new = known_flags(current)
    return [dict(chain=key[0], kind=key[1], before=old[key], after=value)
            for key, value in sorted(new.items()) if key in old and old[key] != value]


def record(db, symbol, exchange, snapshot):
    row = db.execute("SELECT * FROM routes WHERE symbol=? AND exchange=? AND enabled=1", (symbol, exchange)).fetchone()
    if not row:
        return
    previous = json.loads(row["baseline"]) if row["baseline"] else {}
    delta = changes(previous, snapshot)
    if delta:
        db.execute("INSERT INTO events(symbol,exchange,detail,created_at) VALUES (?,?,?,?)",
                   (symbol, exchange, json.dumps(delta), datetime.now(timezone.utc).isoformat()))
    baseline = row["baseline"]
    if snapshot.get("status") == "ok":
        # Retain last known values across partial responses; unknown is not paused.
        merged = known_flags(previous)
        merged.update(known_flags(snapshot))
        chains = {}
        for (chain, field), value in merged.items():
            chains.setdefault(chain, {"chain": chain})[field] = value
        baseline = json.dumps({"chains": list(chains.values())})
    db.execute("UPDATE routes SET snapshot=?,baseline=? WHERE symbol=? AND exchange=?",
               (json.dumps(snapshot, default=str), baseline, symbol, exchange))


def scan(fetch=None):
    if not _scan_lock.acquire(blocking=False):
        return overview()
    try:
        if fetch is None:
            from app.crypto import fetch_coin_transfer_status, transfer_status_to_out
            fetch = lambda ex, symbol: transfer_status_to_out(fetch_coin_transfer_status(ex, symbol, timeout=12, cache_seconds=5))
        with connect() as db:
            routes = [(r["symbol"],r["exchange"]) for r in db.execute("SELECT * FROM routes WHERE enabled=1")]
        def query(route):
            symbol, exchange = route
            try:
                data = fetch(exchange, symbol)
            except Exception:
                data = dict(status="error", message="充提接口查询失败", chains=[])
            # Missing credentials and errors remain visibly unknown.
            data.update(symbol=symbol, exchange=exchange, checkedAt=datetime.now(timezone.utc).isoformat())
            return symbol, exchange, data
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(query, routes))
        with connect() as db:
            for symbol, exchange, data in results:
                record(db, symbol, exchange, data)
            db.execute("DELETE FROM events WHERE created_at < datetime('now','-90 days') AND pushed != 0")
    finally:
        _scan_lock.release()
    return overview()


def overview():
    with connect() as db:
        items = []
        for row in db.execute("SELECT * FROM routes WHERE enabled=1 ORDER BY symbol,exchange"):
            data = json.loads(row["snapshot"]) if row["snapshot"] else dict(status="pending", message="等待首次检查", chains=[])
            data.update(symbol=row["symbol"],exchange=row["exchange"])
            items.append(data)
        events = []
        for row in db.execute("SELECT * FROM events WHERE id IN (SELECT id FROM events WHERE pushed=0 ORDER BY id LIMIT 100) OR id IN (SELECT id FROM events ORDER BY id DESC LIMIT 20) ORDER BY id DESC"):
            events.append(dict(id=row["id"],symbol=row["symbol"],exchange=row["exchange"],changes=json.loads(row["detail"]),createdAt=row["created_at"],pushed=row["pushed"]==1,suppressed=row["pushed"]==2))
    return dict(status="ok",computeLocation="tencent_cloud",intervalSeconds=INTERVAL_SECONDS,
                running=_scan_lock.locked(),items=items,recentEvents=events)


def acknowledge(ids):
    if not isinstance(ids,list) or len(ids)>100 or any(type(i) is not int for i in ids):
        raise ValueError("提醒编号无效")
    with connect() as db:
        db.executemany("UPDATE events SET pushed=1 WHERE id=? AND pushed=0", [(i,) for i in ids])
    return overview()


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    def loop():
        while not _stop.is_set():
            started = time.monotonic()
            try:
                scan()
            except Exception:
                import logging
                logging.getLogger(__name__).exception("transfer watch scan failed")
            _stop.wait(max(1, INTERVAL_SECONDS - (time.monotonic() - started)))
    _thread = threading.Thread(target=loop,name="transfer-watch-cloud",daemon=True)
    _thread.start()


def stop():
    _stop.set()
