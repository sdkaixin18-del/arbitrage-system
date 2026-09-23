"""Bounded in-process read-only I/O diagnostics; no credentials or payloads."""
from collections import Counter, deque
import threading

_lock = threading.Lock()
_data = {}


def record(kind, sample):
    with _lock:
        item = _data.setdefault(kind, {'count': 0, 'success': 0, 'errors': Counter(), 'recent': deque(maxlen=100)})
        item['count'] += 1
        item['success'] += int(sample['success'])
        if not sample['success']:
            item['errors'][sample.get('errorType', 'unknown')] += 1
        item['recent'].append(dict(sample))


def snapshot():
    with _lock:
        result = {}
        for kind, item in _data.items():
            recent = list(item['recent'])
            durations = sorted(x['durationMs'] for x in recent)
            result[kind] = {'count': item['count'], 'success': item['success'], 'errors': dict(item['errors']),
                            'recentCount': len(recent), 'recentP95Ms': durations[max(0, int(len(durations)*.95)-1)],
                            'recent': recent[-5:]}
        return result
