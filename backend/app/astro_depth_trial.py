"""Isolated, authenticated depth-only transport trial; never mounted by main."""
from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hmac
import json
import os
import threading
import time
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import Field

from app import astro_depth_cloud as relay


class TrialRequest(relay.PublicGet):
    requestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")


@dataclass
class Entry:
    fingerprint: str
    created: float
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    error: HTTPException | None = None


class SingleFlight:
    def __init__(self, fetch: Callable, capacity: int = 256, retention: float = 300,
                 max_age_ms: int = 3000, concurrency: int = 2):
        self.fetch = fetch
        self.capacity = capacity
        self.retention = retention
        self.max_age_ms = max_age_ms
        self.entries: dict[str, Entry] = {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(concurrency)
        self.metrics = Counter()

    def run(self, request: TrialRequest):
        public = relay.PublicGet(url=request.url, params=request.params)
        relay.validate_target(public)
        fingerprint = json.dumps(public.model_dump(), sort_keys=True, separators=(",", ":"))
        with self.lock:
            self.metrics["requests"] += 1
            now = time.monotonic()
            for key, item in list(self.entries.items()):
                if item.done.is_set() and now - item.created > self.retention:
                    del self.entries[key]
            entry = self.entries.get(request.requestId)
            owner = entry is None
            if entry is not None:
                if entry.fingerprint != fingerprint:
                    self.metrics["conflicts"] += 1
                    raise HTTPException(409, "Request ID reused with different parameters")
                self.metrics["duplicates"] += 1
            else:
                if len(self.entries) >= self.capacity:
                    self.metrics["capacityRejected"] += 1
                    raise HTTPException(429, "Trial dedup capacity reached")
                entry = Entry(fingerprint, now)
                self.entries[request.requestId] = entry
        if owner:
            acquired = self.slots.acquire(blocking=False)
            try:
                if not acquired:
                    with self.lock:
                        self.metrics["busy"] += 1
                    raise HTTPException(429, "Trial concurrency limit")
                accepted_ms = int(time.time() * 1000)
                with self.lock:
                    self.metrics["upstreamCalls"] += 1
                result = self.fetch(public)
                entry.result = {**result, "requestId": request.requestId,
                                "trialAcceptedAtMs": accepted_ms,
                                "queueWaitMs": max(0, result["requestStartedAtMs"] - accepted_ms)}
            except HTTPException as exc:
                entry.error = exc
            except Exception:
                entry.error = HTTPException(502, "Trial upstream failure")
            finally:
                if acquired:
                    self.slots.release()
                entry.done.set()
        elif not entry.done.wait(timeout=5):
            raise HTTPException(504, "Trial in-flight wait timed out")
        if entry.error is not None:
            raise HTTPException(entry.error.status_code, entry.error.detail, headers=entry.error.headers)
        assert entry.result is not None
        # A duplicate never renews quote time or starts another exchange request.
        if int(time.time() * 1000) - entry.result["receivedAtMs"] > self.max_age_ms:
            with self.lock:
                self.metrics["staleRejected"] += 1
            raise HTTPException(410, "Trial result expired; start a new round")
        return entry.result


flight = SingleFlight(relay.public_get)


def authenticate(authorization: str = Header(default="")):
    token = os.environ.get("ASTRO_DEPTH_TRIAL_TOKEN", "")
    if len(token) < 32 or not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "Unauthorized")


@asynccontextmanager
async def lifespan(_app):
    if len(os.environ.get("ASTRO_DEPTH_TRIAL_TOKEN", "")) < 32:
        raise RuntimeError("Trial requires a strong token")
    yield
    relay._client.close()


app = FastAPI(title="astro-depth-trial", dependencies=[Depends(authenticate)],
              docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.get("/health")
def health():
    with flight.lock:
        return {"service": "astro-depth-trial", "readOnly": True,
                "serverTimeMs": int(time.time() * 1000),
                "metrics": dict(flight.metrics), "entries": len(flight.entries)}


@app.post("/v1/public-get")
def public_get(request: TrialRequest):
    return flight.run(request)
