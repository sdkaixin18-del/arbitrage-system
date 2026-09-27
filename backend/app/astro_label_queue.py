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
    rows = json.loads(p.read_text()) if p.exists() else {}
    if not isinstance(rows, dict) or any(
        not isinstance(row, dict) or not {'id', 'state', 'nextAt', 'pair', 'attempts', 'updatedAt'} <= row.keys()
        for row in rows.values()
    ):
        raise ValueError('Invalid label queue')
    return rows


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


def enqueue_lookup(pair, card):
    cid = str(card.get('id') or '')
    if not cid or pair.get('buyEx') == 'pancakeswapv3':
        return
    with _lock:
        rows = _load()
        if cid in rows:
            return
        rows[cid] = {'id': cid, 'text': '', 'pair': {
            k: pair[k] for k in ('name', 'type', 'buyEx', 'sellEx', '_chainNote', '_dexConfig') if k in pair},
            'state': 'pending_lookup', 'attempts': 0, 'nextAt': time.time() + 10,
            'updatedAt': time.time()}
        _save(rows)


def deliver_one(cards, publish, *, now=None):
    if not _worker.acquire(blocking=False):
        return
    try:
        now = time.time() if now is None else now
        with _lock:
            rows = _load()
            due = [r for r in rows.values() if r['state'] in ('pending', 'needs_review', 'pending_lookup') and r['nextAt'] <= now]
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
            lookup_complete = True
            if row['state'] == 'pending_lookup':
                try:
                    from app.astro_transfer_labels import collect, routes
                    note, evidence = collect(row['pair'])
                    lookup_complete = not routes(row['pair']) or bool(evidence) and all(item.get('status') == 'ok' for item in evidence)
                except Exception:
                    note, evidence = '', []
                    lookup_complete = False
                if note or lookup_complete:
                    row['text'] = '；'.join(filter(None, [row['pair'].get('_chainNote'), note]))
                else:
                    row['text'] = row.get('text') or row['pair'].get('_chainNote', '')
            state, success = ('no_label_needed' if lookup_complete else 'pending_lookup'), False
            if row['text']:
                try:
                    success = row.get('publishedText') == row['text'] or publish(
                        {**row['pair'], '_chainNote': row['text']}, matches[0])
                except Exception:
                    success = False
                if success:
                    row['publishedText'] = row['text']
                state = ('published_to_server' if success else 'pending') if lookup_complete else 'pending_lookup'
        with _lock:
            rows = _load()
            current = rows.get(row['id'])
            if not current or current['updatedAt'] != row['updatedAt']:
                return
            attempts = current['attempts'] + int(same)
            if row['text']:
                current['text'] = row['text']
                current['publishedText'] = row.get('publishedText')
            if same and not success and attempts >= 3 and state == 'pending':
                state = 'needs_review'
            current.update(state=state, attempts=attempts, nextAt=now + (30, 120, 600)[min(max(attempts-1, 0), 2)],
                           updatedAt=time.time())
            _save(rows)
    except Exception as exc:
        # A storage/read failure must leave the durable job intact for another pass.
        from app.astro_sdk import _log
        _log('astro_label_delivery_failed', level='error', message='备注补查队列异常，保留任务等待重试',
             details={'errorType': type(exc).__name__})
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
        if not any(r['state'] in ('pending', 'needs_review', 'pending_lookup') and r['nextAt'] <= time.time() for r in _load().values()):
            return
    threading.Thread(target=deliver_one, args=(cards, publish), name='astro-label-retry', daemon=True).start()


def status():
    try:
        with _lock:
            rows = list(_load().values())
    except Exception as exc:
        return {'state': 'read_failed', 'errorType': type(exc).__name__}
    return {'pending': sum(r['state'] in ('pending', 'needs_review', 'pending_lookup') for r in rows),
            'needsReview': sum(r['state'] == 'needs_review' for r in rows),
            'publishedToServer': sum(r['state'] == 'published_to_server' for r in rows),
            'browserDisplayVerified': False}
