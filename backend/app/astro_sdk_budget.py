"""Process-wide SDK quota shared by reads, writes and confirmation polling."""
import threading
import time
from collections import deque
from urllib.parse import urlsplit


class BudgetDeferred(Exception):
    """No network request was sent; keep the route eligible for a later round."""


class RequestBudget:
    limit = 16  # Official limit is 20/10s; leave room for other IP users.
    window = 10.1

    def __init__(self):
        self.lock = threading.Lock()
        self.sent = deque()
        self.cooldown_until = 0.0
        self.listing_until = 0.0
        self.requests = self.rate_limits = self.deferred = 0
        self.wait_ms = 0.0

    def _delay(self, now, slots, listing):
        while self.sent and now >= self.sent[0] + self.window:
            self.sent.popleft()
        delay = max(0.0, self.cooldown_until - now,
                    self.listing_until - now if listing else 0.0)
        if len(self.sent) + slots > self.limit:
            delay = max(delay, self.sent[len(self.sent) + slots - self.limit - 1] + self.window - now)
        return delay

    def wait(self, deadline, *, consume=False, slots=1, listing=False, no_wait=False):
        started = time.monotonic()
        while True:
            with self.lock:
                now = time.monotonic()
                delay = self._delay(now, slots, listing)
                if now >= deadline or (no_wait and delay > 0):
                    self.deferred += 1
                    raise BudgetDeferred("SDK quota wait deferred before network request")
                if delay <= 0:
                    if consume:
                        self.sent.append(now)
                        self.requests += 1
                    waited = (now - started) * 1000
                    self.wait_ms += waited
                    return waited
                if delay >= deadline - now:
                    self.deferred += 1
                    raise BudgetDeferred("SDK quota exceeds this operation's time budget")
            time.sleep(min(delay, 0.25))

    def limited(self, retry_after=None):
        try:
            delay = max(self.window, min(60.0, float(retry_after)))
        except (TypeError, ValueError):
            delay = self.window
        with self.lock:
            self.rate_limits += 1
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)

    def listing_submitted(self):
        with self.lock:
            self.listing_until = time.monotonic() + 3.0

    def snapshot(self):
        with self.lock:
            self._delay(time.monotonic(), 1, False)
            return {"limit": self.limit, "windowSeconds": self.window,
                    "requests": self.requests, "recentRequests": len(self.sent),
                    "rateLimited": self.rate_limits, "deferred": self.deferred,
                    "waitMs": round(self.wait_ms, 1),
                    "listingIntervalSeconds": 3,
                    "cooldownSeconds": round(max(0, self.cooldown_until - time.monotonic()), 2)}


_lock = threading.Lock()
_budgets = {}


def budget_for(base_url):
    # IP-scoped server limit: API paths and account keys must share a budget.
    key = urlsplit(base_url).hostname or base_url
    with _lock:
        if key not in _budgets:
            _budgets[key] = RequestBudget()
        return _budgets[key]
