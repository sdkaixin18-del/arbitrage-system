"""Durable best-effort label delivery; never creates or modifies trading cards."""
import json
import os
import threading
import time
from pathlib import Path
from app.database import get_data_dir

_lock = threading.Lock()
_worker = threading.Lock()
_last_schedule = 0.0


def _path():
    return Path(get_data_dir()) / 'astro-label-delivery.json'


def _load():
    p = _path()
    return json.loads(p.read_text()) if p.exists() else {}


def _save(rows):
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def enqueue(pair, card):
    cid = str(card.get('id') or '')
    if not cid or not pair.get('_chainNote') or pair.get('buyEx') == 'pancakeswapv3':
        return
    # Whitelist only label identity metadata; no credentials or positions.
    with _lock:
        rows = _load()
        previous = rows.get(cid)
        if previous and previous.get('text') == pair['_chainNote']:
            return
        rows[cid] = {'id': cid, 'text': pair['_chainNote'], 'pair': {
            k: pair[k] for k in ('name', 'type', 'buyEx', 'sellEx', '_chainNote', '_dexConfig') if k in pair},
            'state': 'pending', 'attempts': 0, 'nextAt': 0, 'updatedAt': time.time()}
        _save(rows)


def deliver_one(cards, publish, *, now=None):
    if not _worker.acquire(blocking=False):
        return
    try:
        now = time.time() if now is None else now
        with _lock:
            rows = _load()
            due = [r for r in rows.values() if r['state'] in ('pending', 'needs_review') and r['nextAt'] <= now]
            if not due:
                return
            row = min(due, key=lambda r: r['nextAt'])
            row = json.loads(json.dumps(row))
        matches = [c for c in cards if str(c.get('id') or '') == row['id']]
        same = len(matches) == 1 and all(str(matches[0].get(k) or '').lower() == str(row['pair'].get(k) or '').lower()
                                        for k in ('name', 'type', 'buyEx', 'sellEx'))
        if not same:
            state, success = 'card_absent_or_changed', False
        else:
            try:
                success = publish(row['pair'], matches[0])
            except Exception:
                success = False
            state = 'published_to_server' if success else 'pending'
        with _lock:
            rows = _load()
            current = rows.get(row['id'])
            if not current or current['updatedAt'] != row['updatedAt']:
                return
            attempts = current['attempts'] + int(same)
            if same and not success and attempts >= 3:
                state = 'needs_review'
            current.update(state=state, attempts=attempts, nextAt=now + (30, 120, 600)[min(max(attempts-1, 0), 2)],
                           updatedAt=time.time())
            _save(rows)
    finally:
        _worker.release()


def schedule(cards, publish):
    # Called only after a successful fresh SDK list; at most one worker.
    global _last_schedule
    with _lock:
        now = time.monotonic()
        if _worker.locked() or now - _last_schedule < 5:
            return
        _last_schedule = now
        if not any(r['state'] in ('pending', 'needs_review') and r['nextAt'] <= time.time() for r in _load().values()):
            return
    threading.Thread(target=deliver_one, args=(cards, publish), name='astro-label-retry', daemon=True).start()


def status():
    try:
        with _lock:
            rows = list(_load().values())
    except Exception as exc:
        return {'state': 'read_failed', 'errorType': type(exc).__name__}
    return {'pending': sum(r['state'] in ('pending', 'needs_review') for r in rows),
            'needsReview': sum(r['state'] == 'needs_review' for r in rows),
            'publishedToServer': sum(r['state'] == 'published_to_server' for r in rows),
            'browserDisplayVerified': False}
