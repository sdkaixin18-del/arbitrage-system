"""Route-scoped transport selection, evidence-based alerts and bounded cloud queue.

This module never fetches quotes, creates cards or places orders itself.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time


LABELS = {"capacity": "复核繁忙", "quote_quality": "盘口质量不达标", "transport": "通道连接异常"}


def route_key(pair):
    return "|".join(str(pair.get(k) or "").upper() for k in ("name", "type", "buyEx", "sellEx"))


def category(report):
    reason, error = str(report.get("reason") or ""), str(report.get("error") or "").lower()
    if any(s in error for s in ("深度不足", "insufficient depth", "depth not sufficient")):
        return None
    if reason in {"cloud_depth_busy", "cloud_queue_expired", "local_queue_timeout"} or any(s in error for s in ("429", "并发", "排队", "rate limit")):
        return "capacity"
    if reason in {"stale_direct_quote", "direct_quote_time_skew", "direct_spread_unavailable"} or any(s in error for s in ("陈旧", "时钟偏差", "盘口为空", "深度为空", "盘口时间无效或缺失")):
        return "quote_quality"
    if reason in {"local_depth_deadline_exceeded", "cloud_depth_disabled", "cloud_depth_unsupported",
                  "cex_executable_depth_unavailable", "direct_quote_unavailable", "direct_funding_unavailable",
                  "okxdex_executable_quote_unavailable"}:
        return "transport"
    return None


def utc_stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class RoutePolicy:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self.lock = threading.RLock()
        self.routes = {}
        self.last_fault_attempt = float("-inf")
        self.last_recovery_attempt = float("-inf")
        self.pending = None
        self.storage = None

    def _row(self, pair):
        key = route_key(pair)
        row = self.routes.setdefault(key, {"route": key, "symbol": pair.get("name"), "type": pair.get("type"),
            "buyExchange": pair.get("buyEx"), "sellExchange": pair.get("sellEx"), "mode": "local_proxy",
            "failureSince": None, "healthySince": None, "notified": False, "generation": 0})
        row["seen"] = self.clock()
        if len(self.routes) > 1000:
            old = sorted((r for r in self.routes.values() if not r.get("notified") and r is not row), key=lambda r:r["seen"])
            for item in old[:len(self.routes)-1000]:
                self.routes.pop(item["route"], None)
        return row

    def use_cloud(self, pair):
        with self.lock:
            row = self._row(pair)
            return row.get("cloudSelected", False) and not row.get("localReady", False)

    def activate_cloud(self, pair, sources):
        with self.lock:
            row = self._row(pair)
            changed = not row.get("cloudSelected", False)
            row.update(mode="tencent_cloud", cloudSelected=True, localReady=False, waitingSources=set(sources))
            return changed

    def source_recovered(self, source):
        with self.lock:
            for row in self.routes.values():
                waiting = row.get("waitingSources")
                if waiting is not None:
                    waiting.discard(source)
                    row["localReady"] = not waiting

    def observe(self, pair, report, *, transport, available, alertable=True):
        now = self.clock()
        with self.lock:
            row = self._row(pair)
            row.update(lastReason=str(report.get("reason") or ""), lastCheckedAt=utc_stamp(self.wall()),
                       lastCheck=now, lastDurationMs=report.get("durationMs") or report.get("cloudDurationMs") or report.get("localElapsedMs"))
            if available:
                row["mode"] = transport
                row["cloudSelected"] = transport == "tencent_cloud"
                row["lastSuccessfulVerificationAt"] = utc_stamp(self.wall())
                # Display the latest valid observation immediately. Recovery
                # notification still independently requires ten stable seconds.
                row["category"] = None
                row["error"] = None
                if row.get("healthySince") is None or now-row.get("lastHealthy", now)>6:
                    row["healthySince"] = now
                row["lastHealthy"] = now
                # Any valid response breaks failure continuity, even if there is no opportunity.
                row["failureSince"] = None
                if now-row["healthySince"] >= 10:
                    row["category"] = None
                    row["error"] = None
            else:
                kind = category(report) or "transport"
                if row.get("failureSince") is None or now-row.get("lastFailure", now)>6 or row.get("category") != kind:
                    row["failureSince"] = now
                    row["generation"] += 1
                row.update(mode="paused", category=kind, alertable=alertable and kind != "quote_quality", error=str(report.get("error") or report.get("reason"))[:350],
                           lastFailure=now, healthySince=None)

    def _eligible(self, row, kind, now):
        if kind == "fault":
            return row.get("category") != "quote_quality" and row.get("alertable", True) and row.get("failureSince") is not None and now-row["failureSince"] >= 20 and now-row.get("lastFailure", 0)<=6
        return row.get("notified") and row.get("healthySince") is not None and now-row["healthySince"]>=10 and now-row.get("lastHealthy", 0)<=6

    def tick(self):
        with self.lock:
            if self.pending:
                return None
            now = self.clock()
            for kind, due in (("fault", now-self.last_fault_attempt>=600), ("recovery", now-self.last_recovery_attempt>=10)):
                rows = [r for r in self.routes.values() if due and self._eligible(r, kind, now)]
                if rows:
                    action = {"kind":kind, "routes":[r["route"] for r in rows]}
                    self.pending = action
                    return action
            return None

    def prepare(self, action):
        with self.lock:
            if self.pending is not action:
                return None
            now = self.clock()
            rows = [self.routes[k] for k in action["routes"] if k in self.routes and self._eligible(self.routes[k], action["kind"], now)]
            if not rows:
                self.pending = None
                return None
            if action["kind"] == "fault":
                self.last_fault_attempt = now
            else:
                self.last_recovery_attempt = now
            action["sentRoutes"] = [r["route"] for r in rows]
            action["generations"] = {r["route"]:r["generation"] for r in rows}
            payload = [{"route":r["route"], "category":r.get("category"), "error":r.get("error"),
                        "durationSeconds":round(now-(r.get("failureSince") if r.get("failureSince") is not None else r.get("healthySince", now)), 1)} for r in rows]
            self._save()
            return payload

    def delivered(self, action, ok):
        with self.lock:
            if ok:
                for key in action.get("sentRoutes", []):
                    row = self.routes.get(key)
                    if row is not None:
                        if action["kind"] == "fault":
                            row["notified"] = True
                        elif row["generation"] == action["generations"][key]:
                            row["notified"] = False
            self.pending = None
            self._save()

    def configure_storage(self, path: Path):
        with self.lock:
            self.storage = path
            try:
                data = json.loads(path.read_text())
                now, wall = self.clock(), self.wall()
                for key in ("last_fault_attempt", "last_recovery_attempt"):
                    saved = data.get(key)
                    if isinstance(saved, (int,float)):
                        setattr(self, key, now-max(0,wall-saved))
                for saved in data.get("notified", [])[:250]:
                    pair = dict(zip(("name","type","buyEx","sellEx"), saved["route"].split("|")))
                    row = self._row(pair)
                    row.update(notified=True, category=saved.get("category"), mode="paused")
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _save(self):
        if self.storage is None:
            return
        now, wall = self.clock(), self.wall()
        data = {key:wall-(now-getattr(self,key)) for key in ("last_fault_attempt","last_recovery_attempt") if getattr(self,key)!=float("-inf")}
        data["notified"] = [{"route":r["route"],"category":r.get("category")} for r in self.routes.values() if r.get("notified")][:250]
        try:
            self.storage.parent.mkdir(parents=True, exist_ok=True)
            temp=self.storage.with_suffix(".tmp")
            temp.write_text(json.dumps(data), encoding="utf-8")
            temp.replace(self.storage)
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            now = self.clock()
            fresh = lambda r: now-r.get("lastCheck", float("-inf")) <= 6
            affected = lambda r: r.get("mode") == "paused" and fresh(r) and r.get("failureSince") is not None
            waiting = lambda r: r.get("mode") == "paused" and not fresh(r)
            rows = sorted(self.routes.values(), key=lambda r:(not affected(r), not fresh(r), -r.get("lastCheck", float("-inf"))))
            cloud = lambda r: r.get("mode") == "tencent_cloud" and fresh(r)
            active_rows = [r for r in rows if affected(r) or cloud(r)]
            return {"faultDelaySeconds":20, "globalPushIntervalSeconds":600, "recoveryStableSeconds":10,
                    "quoteQualityPushEnabled":False,
                    "affectedRouteCount":sum(affected(r) for r in rows),
                    "cloudRouteCount":sum(cloud(r) for r in rows),
                    "activeSymbols":sorted({r["symbol"] for r in active_rows if r.get("symbol")}),
                    "activeSources":sorted({r[k] for r in active_rows for k in ("buyExchange", "sellExchange") if r.get(k)}),
                    "awaitingRecheckRouteCount":sum(waiting(r) for r in rows),
                    "routes":[{k:r.get(k) for k in ("route","symbol","type","buyExchange","sellExchange","mode","category","error","lastReason","lastCheckedAt","lastSuccessfulVerificationAt","lastDurationMs")}
                              | {"durationSeconds":round((now if fresh(r) else r.get("lastFailure", r["failureSince"]))-r["failureSince"],1) if r.get("failureSince") is not None else 0,
                                 "mode":"awaiting_recheck" if waiting(r) else r["mode"],
                                 "evidenceFresh":fresh(r)} for r in rows[:50]]}

    def priority(self, pair):
        with self.lock:
            row = self.routes.get(route_key(pair))
            return 0 if pair.get("_hotDirectHit") or not row or not row.get("lastCheckedAt") else 1


class QueueBusy(RuntimeError):
    pass


class LatestQueue:
    """Two workers, at most 16 pending routes, one latest pending job per route."""
    def __init__(self, workers=2, capacity=16, ttl=1.0):
        self.cv=threading.Condition()
        self.workers,self.capacity,self.ttl=workers,capacity,ttl
        self.pending={}
        self.active=0
        self.threads=[]
        self.stopped=False
        self.metrics={"coalesced":0,"expired":0,"rejected":0,"executed":0}

    def run(self,key,fn,*,priority=1):
        with self.cv:
            if self.stopped:
                raise QueueBusy("扫描器停止")
            if not self.threads:
                self.stopped=False
                self.threads=[threading.Thread(target=self._work,name="astro-cloud-route",daemon=True) for _ in range(self.workers)]
                for thread in self.threads:thread.start()
            if key in self.pending:
                job=self.pending[key]
                job.update(fn=fn,priority=min(priority,job["priority"]))
                self.metrics["coalesced"]+=1
            else:
                if len(self.pending)>=self.capacity:
                    self.metrics["rejected"]+=1
                    raise QueueBusy("云端复核有界队列已满")
                job={"fn":fn,"priority":priority,"created":time.monotonic(),"future":Future()}
                self.pending[key]=job
            self.cv.notify_all()
            while not job.get("started") and not job["future"].done():
                remaining=job["created"]+self.ttl-time.monotonic()
                if remaining<=0:
                    if self.pending.get(key) is job:
                        self.pending.pop(key)
                    self.metrics["expired"]+=1
                    job["future"].set_exception(QueueBusy("云端排队超过1秒，等待新行情重新复核"))
                    break
                self.cv.wait(timeout=remaining)
        try:
            return deepcopy(job["future"].result(timeout=max(.01,6-(time.monotonic()-job["created"]))))
        except TimeoutError as exc:
            raise RuntimeError("cloud route response timeout，本轮放弃迟到结果") from exc

    def _work(self):
        while True:
            with self.cv:
                self.cv.wait_for(lambda:self.stopped or self.pending)
                if self.stopped:return
                key=min(self.pending,key=lambda k:(self.pending[k]["priority"],self.pending[k]["created"]))
                job=self.pending.pop(key)
                if time.monotonic()-job["created"]>self.ttl:
                    self.metrics["expired"]+=1
                    job["future"].set_exception(QueueBusy("云端排队超过1秒，等待新行情重新复核"))
                    continue
                self.active+=1
                job["started"]=time.monotonic()
                self.cv.notify_all()
                self.metrics["executed"]+=1
            try:
                result=job["fn"]()
            except Exception as exc:
                job["future"].set_exception(exc)
            else:
                job["future"].set_result(result)
            finally:
                with self.cv:self.active-=1

    def stop(self):
        with self.cv:
            self.stopped=True
            for job in self.pending.values():job["future"].set_exception(QueueBusy("扫描器停止"))
            self.pending.clear()
            self.cv.notify_all()
        for thread in self.threads:thread.join(timeout=2)

    def snapshot(self):
        with self.cv:return {**self.metrics,"active":self.active,"waiting":len(self.pending),"maxActive":self.workers,"maxWaiting":self.capacity,"queueTtlSeconds":self.ttl}
