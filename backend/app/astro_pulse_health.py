"""Independent Pulse-source backoff, with two successes to clear failure state."""
import threading
import time

_lock = threading.Lock()
_sources = {}


def due(url):
    with _lock:
        return time.monotonic() >= _sources.get(url, {}).get('nextAt', 0)


def result(url, started, error=None):
    with _lock:
        row = _sources.setdefault(url, {'requests': 0, 'successes': 0, 'failures': 0, 'recoveryStreak': 0})
        row['requests'] += 1
        row['durationMs'] = round((time.monotonic()-started)*1000, 1)
        if error is None:
            row['successes'] += 1
            row['recoveryStreak'] += 1
            if row['recoveryStreak'] >= 2:
                row['failures'] = 0
            row.update(nextAt=0, errorType=None, category=None)
        else:
            row['failures'] += 1
            row['recoveryStreak'] = 0
            name = type(error).__name__
            row.update(nextAt=time.monotonic()+(2, 5, 15, 30)[min(row['failures']-1, 3)], errorType=name,
                       category='timeout' if 'Timeout' in name else 'connection' if 'Connect' in name else 'invalid_response')


def snapshot():
    with _lock:
        return {url: {**{k:v for k,v in row.items() if k!='nextAt'},
                      'retryAfterSeconds': max(0, round(row.get('nextAt',0)-time.monotonic(),1))} for url,row in _sources.items()}
