from __future__ import annotations

import threading
import time
import sqlite3
import json
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import SessionLocal, get_data_dir
from app.funding_cloud_client import funding_cloud_enabled, funding_cloud_request
from app.models import CryptoFundingCapWatchItem

POLL_SECONDS = 5
_synced_routes = None
_stop = threading.Event()
_lock = threading.Lock()
_thread: threading.Thread | None = None
_cache = dict(status="pending",computeLocation="tencent_cloud",items=[],recentEvents=[],intervalSeconds=30)


def overview():
    return dict(_cache)


def refresh(force=False):
    global _cache, _synced_routes
    if not _lock.acquire(blocking=False):
        return overview()
    try:
        if not funding_cloud_enabled():
            _cache = dict(status="error",message="请启用腾讯云查询服务",items=[],recentEvents=[])
            return overview()
        from app.crypto import funding_cap_watch_item_exchanges, EXCHANGE_NAMES
        from app.notifications import send_bark_or_log
        with SessionLocal() as db:
            items = [dict(symbol=r.symbol,selectedExchanges=funding_cap_watch_item_exchanges(r))
                     for r in db.scalars(select(CryptoFundingCapWatchItem).where(CryptoFundingCapWatchItem.enabled.is_(True)))]
        routes_key = sorted((i["symbol"],tuple(sorted(i["selectedExchanges"]))) for i in items)
        if routes_key != _synced_routes:
            data = funding_cloud_request("PUT", "/v1/transfer-watch", payload={"items":items})
            _synced_routes = routes_key
        else:
            data = funding_cloud_request("GET", "/v1/transfer-watch")
        if force or any(i.get("status")=="pending" for i in data.get("items",[])):
            data = funding_cloud_request("POST", "/v1/transfer-watch/refresh")
        active = {(i["symbol"],ex) for i in items for ex in i["selectedExchanges"]}
        ledger_path = get_data_dir() / "transfer-bark-deliveries.db"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger = sqlite3.connect(ledger_path, timeout=5)
        try:
            ledger.execute("CREATE TABLE IF NOT EXISTS delivered (event_key TEXT PRIMARY KEY)")
            delivered = {r[0] for r in ledger.execute("SELECT event_key FROM delivered")}
        finally:
            ledger.close()
        for event in reversed(data.get("recentEvents",[])):
            if event.get("pushed") or event.get("suppressed") or (event["symbol"],event["exchange"]) not in active:
                continue
            event_key = json.dumps([event['id'],event['symbol'],event['exchange'],event.get('createdAt')])
            if event_key in delivered:
                funding_cloud_request("POST", "/v1/transfer-watch/ack",payload={"ids":[event["id"]]})
                event["pushed"] = True
                continue
            lines = [f"{event['symbol']} · {EXCHANGE_NAMES.get(event['exchange'],event['exchange'])}"]
            for change in event["changes"]:
                action = "充值" if change["kind"]=="depositEnabled" else "提现"
                state = "恢复" if change["after"] else "暂停"
                lines.append(f"{change['chain']}：{action}{state}")
            status, detail = send_bark_or_log(enabled=True,title=f"{event['symbol']} 充提状态变动",
                body="\n".join(lines),group="交易监控·充提状态",url=None,disabled_message="充提变动未推送")
            if status=="ok":
                ledger = sqlite3.connect(ledger_path, timeout=5)
                try:
                    with ledger:
                        ledger.execute("INSERT OR IGNORE INTO delivered VALUES (?)", (event_key,))
                finally:
                    ledger.close()
                funding_cloud_request("POST", "/v1/transfer-watch/ack",payload={"ids":[event["id"]]})
                event["pushed"]=True
            else:
                event["pushError"]=detail
        _cache = {**data,"syncedAt":datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        _cache = {**_cache,"status":"error","message":str(exc)}
    finally:
        _lock.release()
    return overview()


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    def loop():
        while not _stop.is_set():
            started = time.monotonic()
            refresh()
            _stop.wait(max(1, POLL_SECONDS - (time.monotonic() - started)))
    _thread=threading.Thread(target=loop,name="transfer-watch-cloud-bridge",daemon=True)
    _thread.start()


def stop():
    _stop.set()
