"""Local news -> durable market-scoped opening blocks and bounded listing discovery.

News and SDK polling run on one background worker, never in a quote worker.
Removing a notice from the monitor's bounded feed does not revoke its block.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import re
import threading
import time
from urllib.parse import urlparse, parse_qsl, urlencode

URL = 'http://127.0.0.1:8790/api/monitor'
INTERVAL = 30
EXCHANGES = {'bn': 'binance', 'bg': 'bitget', 'by': 'bybit', 'gt': 'gate', 'as': 'aster'}
DOMAINS = {'binance': ('binance.com',), 'bitget': ('bitget.com',), 'bybit': ('bybit.com',),
           'gate': ('gate.com', 'gate.io'), 'okx': ('okx.com',), 'aster': ('asterdex.com',),
           'hl': ('hyperliquid.xyz',)}
_lock = threading.RLock()
_stop = threading.Event()
_thread = None
_startup_pending = False
_path = None
_storage_loaded = True
_blocks = {}
_listings = {}
_completed_listing_events = set()
_trading_markets = {}
_label_published_ids = set()
_state = {'lastReadAt': None, 'sourceUpdatedAt': None, 'lastError': None,
          'storageError': None, 'cardChecks': [], 'cardCheckError': None,
          'lastCardCheckAt': None, 'unresolvedNoticeCount': 0}


def exchange(value):
    raw = str(value or '').strip().lower()
    return EXCHANGES.get(raw, raw)


def market(value):
    return 'future' if value in {'contract', 'future', 'futures'} else 'spot' if value == 'spot' else None


def symbol(value):
    return str(value or '').strip().upper().removesuffix('USDT')


def timestamp(value):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return date.timestamp() if date.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def official_url(ex, value):
    parsed = urlparse(str(value or ''))
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and any(host == d or host.endswith('.'+d) for d in DOMAINS.get(ex, ()))


def _canonical_listing_url(value):
    """Language variants of one official article represent the same event."""
    parsed = urlparse(str(value or ''))
    host = (parsed.hostname or '').removeprefix('www.')
    if host == 'gate.io':
        host = 'gate.com'
    path = re.sub(r'^/(?:[a-z]{2}(?:-[a-z]{2})?)(?=/)', '', parsed.path, flags=re.I)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parsed.query)
                             if not k.lower().startswith('utm_') and k.lower() not in {'lang', 'locale'}))
    return f'https://{host}{path.rstrip("/")}' + ('?' + query if query else '')


def _service_expansion_notice(row):
    title = str(row.get('title') or row.get('announcementTitle') or '')
    # Spot margin, lending and bots are not a new underlying spot market.
    # A combined announcement explicitly launching spot/futures remains valid.
    expansion = re.search(r'现货杠杆|杠杆(?:新增|交易|借贷)|新增.*(?:杠杆|借贷)|margin|lending|trading bots?', title, re.I)
    underlying = re.search(r'(?:上线|上架|开放).*?(?:现货交易|合约交易)|(?:spot|perpetual|futures)\s+(?:listing|launch)|list.*?(?:perpetual|futures)', title, re.I)
    return bool(expansion and not underlying)


def _dated_delivery_contract_notice(row):
    """Reject dated futures announcements from new-market card creation.

    Astro cards model spot and perpetual futures.  Quarterly or other dated
    delivery contracts can mention an established asset such as BTC while
    introducing only a new expiry.  Treating those notices as an underlying
    listing creates unrelated SF/FF cards across every live venue.
    """
    kind = str(row.get('contractKind') or row.get('contractType') or '').strip().lower()
    if kind in {'delivery', 'dated', 'quarterly', 'quarter', 'expiry', 'expiring'}:
        return True
    title = str(row.get('title') or row.get('announcementTitle') or '')
    if re.search(r'永续|perpetual', title, re.I):
        return False
    return bool(re.search(
        r'交割合约|季度合约|当季|次季|quarterly\s+(?:delivery\s+)?futures?|dated\s+futures?|delivery\s+futures?',
        title,
        re.I,
    ))


def _observed_live(row, now):
    seen = _trading_markets.get((row['symbol'], row['exchange'], row['market']))
    return bool(seen and -5 <= now - seen['observedAt'] <= 30
                and (row.get('launchAt') is None or row['launchAt'] <= now))


def parse_snapshot(payload, *, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or not isinstance(payload.get('announcements'), list) or not isinstance(payload.get('listingReminders'), list):
        raise ValueError('invalid_news_snapshot')
    stamp = timestamp(payload.get('updatedAt'))
    if stamp is None or stamp > now + 60:
        raise ValueError('invalid_news_timestamp')
    blocks, listings, unresolved = {}, {}, 0
    for kind in ('announcements', 'listingReminders'):
        for row in payload[kind]:
            if not isinstance(row, dict):
                continue
            action = row.get('action') or ('listing' if kind == 'listingReminders' else None)
            ex, mk, coin = exchange(row.get('exchange')), market(row.get('marketType')), symbol(row.get('symbol'))
            url = row.get('url') or row.get('announcementUrl')
            trusted = (row.get('detailStatus') == 'parsed' if kind == 'announcements' else row.get('announcementMatched') is True)
            valid = trusted and mk and coin and re.fullmatch(r'[\w.-]{1,50}', coin) and official_url(ex, url)
            if action == 'delisting':
                if not valid:
                    unresolved += 1
                    continue
                key = (coin, ex, mk)
                evidence = {'symbol': coin, 'exchange': ex, 'market': mk, 'sourceUrl': url,
                            'publishedAt': row.get('publishedAt'), 'scheduledAt': row.get('scheduledAt'),
                            'openingSuspendsAt': row.get('openingSuspendsAt'),
                            'title': row.get('title') or row.get('announcementTitle'),
                            'source': 'local_exchange_news', 'reason': 'news_delisting_announced', 'decision': 'reject'}
                # Reminders carry openingSuspendsAt; do not lose it to a sparse duplicate.
                blocks[key] = {**blocks.get(key, {}), **{k: v for k, v in evidence.items() if v is not None}}
            elif action == 'listing' and valid and now - stamp <= 180:
                if _service_expansion_notice(row) or _dated_delivery_contract_notice(row):
                    continue
                if row.get('recoveredAfterGap') or row.get('notificationPolicy') == 'historical_only' or row.get('assetType') == 'stock':
                    continue
                published = timestamp(row.get('publishedAt'))
                scheduled = timestamp(row.get('scheduledAt'))
                # A future launch remains eligible even if announced days ago.
                # Unknown launch time gets a bounded 24h window from publication.
                if published is not None and published > now + 60:
                    continue
                if published is None and scheduled is None:
                    continue
                until = scheduled if scheduled is not None else published + 86400
                if now >= until:
                    continue
                listings[(coin, ex, mk)] = {'symbol': coin, 'exchange': ex, 'market': mk,
                    'sourceUrl': url, 'expiresAt': until, 'launchAt': scheduled, 'publishedAt': published or 0,
                    'title': row.get('title') or row.get('announcementTitle') or ''}
    return blocks, listings, unresolved


def _save():
    if _path is None:
        return
    if not _storage_loaded:
        raise ValueError('restore_corrupt_block_storage_first')
    _path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'version': 3, 'blocks': list(_blocks.values()), 'listings': list(_listings.values()), 'completedListings': sorted(_completed_listing_events)}, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, _path)


def configure(path):
    global _path, _blocks, _listings, _storage_loaded, _completed_listing_events
    with _lock:
        _path = path
        try:
            saved = json.loads(path.read_text()) if path.exists() else {'blocks': []}
            rows = saved['blocks']
            if not isinstance(rows, list):
                raise ValueError('invalid_block_storage')
            parsed = {}
            for row in rows:
                if not isinstance(row, dict) or not row.get('symbol') or not market(row.get('market')) or not official_url(row.get('exchange'), row.get('sourceUrl')):
                    raise ValueError('invalid_block_storage')
                parsed[(row['symbol'], row['exchange'], row['market'])] = row
            completed = saved.get('completedListings', [])
            if not isinstance(completed, list) or not all(isinstance(k, str) for k in completed):
                raise ValueError('invalid_completed_listing_storage')
            _completed_listing_events = set()
            for key in completed:
                parts = json.loads(key)
                if not isinstance(parts, list) or len(parts) != 4:
                    raise ValueError('invalid_completed_listing_key')
                _completed_listing_events.add(_listing_event_key(dict(zip(('symbol','exchange','market','sourceUrl'), parts))))
            saved_listings = saved.get('listings', [])
            if not isinstance(saved_listings, list):
                raise ValueError('invalid_listing_storage')
            restored = {}
            for row in saved_listings:
                if not isinstance(row, dict) or not row.get('symbol') or not market(row.get('market')) or not official_url(row.get('exchange'), row.get('sourceUrl')):
                    raise ValueError('invalid_listing_storage')
                if float(row.get('expiresAt', 0)) > time.time() and not _dated_delivery_contract_notice(row):
                    restored[(row['symbol'], row['exchange'], row['market'])] = row
            _listings = restored
            _blocks = parsed
            _storage_loaded = True
            _state['storageError'] = None
        except Exception as exc:
            _storage_loaded = False
            _state['storageError'] = type(exc).__name__


def ingest(payload, *, now=None):
    global _listings
    blocks, listings, unresolved = parse_snapshot(payload, now=now)
    now = time.time() if now is None else now
    with _lock:
        changed = any(_blocks.get(k) != v for k, v in blocks.items())
        _blocks.update(blocks)
        merged_listings = {k: v for k, v in _listings.items() if v['expiresAt'] > now}
        merged_listings.update(listings)
        completed_before = set(_completed_listing_events)
        for kind in ('announcements', 'listingReminders'):
            for row in payload.get(kind, []):
                if not isinstance(row, dict) or (row.get('action') or 'listing') != 'listing':
                    continue
                ex, mk, coin = exchange(row.get('exchange')), market(row.get('marketType')), symbol(row.get('symbol'))
                url = row.get('url') or row.get('announcementUrl')
                if not mk or not coin or not official_url(ex, url):
                    continue
                scheduled = timestamp(row.get('scheduledAt'))
                traded = timestamp(row.get('tradableAt'))
                title = str(row.get('title') or row.get('announcementTitle') or '')
                completed = row.get('hasOccurred') is True or (scheduled is not None and scheduled <= now) or (traded is not None and traded <= now)
                completed = completed or bool(re.search(r'已上线|已开放.*交易|\bnow live\b|\bis live\b', title, re.I)) or _service_expansion_notice(row) or _dated_delivery_contract_notice(row)
                if completed:
                    _completed_listing_events.add(_listing_event_key({'symbol':coin,'exchange':ex,'market':mk,'sourceUrl':url,'publishedAt':timestamp(row.get('publishedAt')) or 0}))
        for row in merged_listings.values():
            if (row.get('launchAt') is not None and row['launchAt'] <= now) or _observed_live(row, now) or _service_expansion_notice(row) or _dated_delivery_contract_notice(row):
                _completed_listing_events.add(_listing_event_key(row))
        merged_listings = {k:v for k,v in merged_listings.items() if _listing_event_key(v) not in _completed_listing_events}
        changed = changed or merged_listings != _listings or completed_before != _completed_listing_events
        _listings = merged_listings  # Preserve future announcements when the feed rotates.
        if changed or _state.get('storageError'):
            try:
                _save()
                _state['storageError'] = None
            except Exception as exc:
                _state['storageError'] = type(exc).__name__
        _state.update(lastReadAt=datetime.now(timezone.utc).isoformat(), sourceUpdatedAt=payload['updatedAt'],
                      lastError='news_snapshot_stale' if now - timestamp(payload['updatedAt']) > 180 else None,
                      unresolvedNoticeCount=unresolved)


def legs(pair_type, buy, sell):
    types = {'SF': ('spot', 'future'), 'FF': ('future', 'future'), 'FR': ('future', 'future'),
             'FS': ('future', 'spot'), 'SR': ('spot', 'future')}
    markets = types.get(str(pair_type).upper())
    return list(zip((exchange(buy), exchange(sell)), markets)) if markets else []


def route_check(coin, pair_type, buy, sell):
    with _lock:
        for ex, mk in legs(pair_type, buy, sell):
            match = _blocks.get((symbol(coin), ex, mk))
            if match:
                return deepcopy(match)
        if _startup_pending:
            return {'decision': 'observe', 'reason': 'news_policy_initializing'}
        if _state.get('storageError'):
            return {'decision': 'observe', 'reason': 'news_block_storage_unavailable'}
    return None


def _listing_event_key(row):
    return json.dumps([row.get(k) for k in ('symbol','exchange','market')] + [_canonical_listing_url(row.get('sourceUrl'))], ensure_ascii=False)


def active_listings():
    with _lock:
        # Short network failure does not extend lifetime; prolonged outage stops listing work.
        if time.time() - (timestamp(_state.get('sourceUpdatedAt')) or 0) > 180:
            return []
        now = time.time()
        rows = sorted((row for row in _listings.values() if now < row['expiresAt'] and (row.get('launchAt') is None or now < row['launchAt']) and _listing_event_key(row) not in _completed_listing_events and not _observed_live(row, now) and not _service_expansion_notice(row) and not _dated_delivery_contract_notice(row)), key=lambda row: row['publishedAt'], reverse=True)
        return deepcopy(rows)


def status():
    with _lock:
        return {**deepcopy(_state), 'running': bool(_thread and _thread.is_alive()), 'intervalSeconds': INTERVAL,
                'blockCount': len(_blocks), 'blocks': deepcopy(list(_blocks.values())),
                'listingSymbols': sorted({row['symbol'] for row in active_listings()}),
                'sourceUrl': URL, 'newCardsPaused': True, 'listingUsesExistingRules': False, 'listingCardMode': 'announcement_paused',
                'listingRoutes': deepcopy(_state.get('listingRoutes', [])),
                'listingScheduleError': _state.get('listingScheduleError'),
                'lastListingScheduleAt': _state.get('lastListingScheduleAt')}


def enforce_cards(client):
    """Only turn disableOpen on; retain every other latest card setting.

    API update requires the full pair. Re-read before each update and read back
    afterwards. A failed/uncertain write is retried next cycle from live state.
    """
    pairs = client.list_pairs(deadline=time.monotonic() + 8)
    results = []
    budget = time.monotonic() + 45
    for pair in pairs:
        block = route_check(pair.get('name'), pair.get('type'), pair.get('buyEx'), pair.get('sellEx'))
        if not block or block.get('decision') != 'reject':
            continue
        result = {'id': pair.get('id'), 'symbol': pair.get('name'), 'type': pair.get('type'),
                  'buyExchange': pair.get('buyEx'), 'sellExchange': pair.get('sellEx'),
                  'sourceUrl': block.get('sourceUrl'), 'status': 'pending'}
        try:
            if pair.get('disableOpen') is not True and time.monotonic() >= budget:
                result['error'] = 'cycle_budget_exhausted'
                results.append(result)
                continue
            if pair.get('disableOpen') is True:
                result['status'] = 'confirmed'
            else:
                live = next((p for p in client.list_pairs(deadline=time.monotonic()+8) if p.get('id') == pair.get('id')), None)
                if live is None:
                    result['status'] = 'removed'
                elif not route_check(live.get('name'), live.get('type'), live.get('buyEx'), live.get('sellEx')):
                    result['status'] = 'route_changed'
                else:
                    if live.get('disableOpen') is not True:
                        client.request({'action': 'update', 'pair': {**live, 'disableOpen': True}}, deadline=time.monotonic()+8)
                    saved = next((p for p in client.list_pairs(deadline=time.monotonic()+8) if p.get('id') == live['id']), None)
                    if not saved or saved.get('disableOpen') is not True:
                        raise ValueError('disable_open_not_confirmed')
                    if any(saved.get(k) != live.get(k) for k in ('status', 'disableClose', 'closePosition')):
                        raise ValueError('closing_settings_changed_during_update')
                    result['status'] = 'confirmed'
        except Exception as exc:
            result['error'] = type(exc).__name__
        results.append(result)
    ensure_announcement_labels(pairs)
    return results


def _cycle():
    global _startup_pending
    import httpx
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get(URL)
            response.raise_for_status()
            ingest(response.json())
    except Exception as exc:
        with _lock:
            _state['lastError'] = type(exc).__name__
    finally:
        with _lock:
            _startup_pending = False
    from app.astro_sdk import AstroSdkClient, astro_sdk_config
    config = astro_sdk_config()
    if not config.configured or config.dry_run:
        return
    try:
        with _lock:
            has_blocks = bool(_blocks)
        from app.astro_card_registry import auto_created_route_records
        has_announcement_cards = any(r.get('announcementPrecreated') for r in auto_created_route_records())
        if has_blocks or has_announcement_cards:
            with AstroSdkClient(config) as client:
                checks = enforce_cards(client)
            with _lock:
                changed = checks != _state.get('cardChecks')
                _state.update(cardChecks=checks, cardCheckError=None, lastCardCheckAt=datetime.now(timezone.utc).isoformat())
            if changed:
                from app.system_runtime_log import append_system_runtime_event
                append_system_runtime_event('astro_news_card_opening_check', level='warning' if any(r['status']=='pending' for r in checks) else 'info',
                    source='backend', module='astro_news_policy', message='下架公告关联卡片已核对禁止开仓状态；原平仓设置保留。', details={'cards': checks})
    except Exception as exc:
        with _lock:
            _state['cardCheckError'] = type(exc).__name__
    if config.enabled:
        try:
            schedule_listing_cards(config)
        except Exception as exc:
            with _lock:
                _state['listingScheduleError'] = type(exc).__name__


def _loop():
    while not _stop.is_set():
        try:
            _cycle()
        except Exception as exc:
            with _lock:
                _state['lastError'] = type(exc).__name__
        _stop.wait(INTERVAL)


def start():
    global _thread, _startup_pending
    if _thread and _thread.is_alive():
        return
    from app.database import get_data_dir
    configure(get_data_dir() / 'system-runtime' / 'astro-news-blocks.json')
    _startup_pending = True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='astro-news-policy', daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=2)


def update_trading_markets(payloads):
    """Reuse the scanner's fresh CEX market observations; no new API request."""
    global _trading_markets
    from app import astro_spread_scanner as scanner
    now = time.time()
    selected = scanner.spread_scan_market_keys()
    aliases = scanner._load_pulse_symbol_aliases()
    markets = {}
    for payload in payloads:
        data = payload.get('data', {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if key not in selected or not isinstance(value, dict):
                continue
            route = scanner._pulse_exchange_market(key)
            at = scanner._finite(value.get('ts'))
            if not route or route[0] not in DOMAINS or at is None or not -5 <= now - at/1000 <= 30:
                continue
            ex, mk = route
            for row in value.get('list', []):
                if not isinstance(row, dict):
                    continue
                raw = symbol(row.get('name'))
                ask, bid = scanner._finite(row.get('a')), scanner._finite(row.get('b'))
                if not raw or ask is None or bid is None or not 0 < bid <= ask:
                    continue
                coin, _ = aliases.get((ex, mk, raw), (raw, 1))
                markets[(coin, ex, mk)] = {'symbol': coin, 'exchange': ex, 'market': mk, 'observedAt': at/1000}
    with _lock:
        _trading_markets = markets
        changed = False
        for key, row in list(_listings.items()):
            # A pre-open indicative book must not end a known future event.
            if key in markets and (row.get('launchAt') is None or row['launchAt'] <= now):
                _completed_listing_events.add(_listing_event_key(row))
                del _listings[key]
                changed = True
        if changed:
            try:
                _save()
            except Exception as exc:
                _state['storageError'] = type(exc).__name__


def listing_route_specs(notices, trading, selected):
    """Announcement+announcement or announcement+live, using exact market types."""
    from app.astro_sdk import ASTRO_FF_BUY_EXCHANGES, ASTRO_FF_SELL_EXCHANGES
    from app.astro_spread_scanner import _sf_route_supported
    by_coin = {}
    coins = {row['symbol'] for row in notices}
    for row in trading:
        if row['symbol'] not in coins:
            continue
        leg = (row['exchange'], row['market'])
        if leg in selected:
            by_coin.setdefault(row['symbol'], {})[leg] = {**row, 'announced': False}
    for row in notices:
        leg = (row['exchange'], row['market'])
        if leg in selected:
            by_coin.setdefault(row['symbol'], {})[leg] = {**row, 'announced': True}
    result = []
    for coin, legs_by_market in sorted(by_coin.items()):
        for (buy, buy_market), left in sorted(legs_by_market.items()):
            for (sell, sell_market), right in sorted(legs_by_market.items()):
                if not (left['announced'] or right['announced']) or sell_market != 'future':
                    continue
                kind = 'SF' if buy_market == 'spot' else 'FF'
                if kind == 'FF' and (buy == sell or buy not in ASTRO_FF_BUY_EXCHANGES or sell not in ASTRO_FF_SELL_EXCHANGES):
                    continue
                if kind == 'SF' and not _sf_route_supported(buy, sell):
                    continue
                if route_check(coin, kind, buy, sell):
                    continue
                result.append({'symbol': coin, 'type': kind, 'buyExchange': buy, 'sellExchange': sell,
                    'buyMarket': buy_market, 'sellMarket': sell_market,
                    'category': 'both_announced' if left['announced'] and right['announced'] else 'live_and_announced',
                    'announcements': [r['sourceUrl'] for r in (left, right) if r['announced']]})
    return result


def current_listing_routes():
    from app import astro_spread_scanner as scanner
    selected = {(ex, mk) for key, ex, _, mk in scanner.PULSE_MARKETS
                if key in scanner.spread_scan_market_keys() and ex in DOMAINS}
    with _lock:
        trading = [deepcopy(row) for row in _trading_markets.values() if -5 <= time.time()-row['observedAt'] <= 30]
    return listing_route_specs(active_listings(), trading, selected)


def announcement_submit_guard(pair):
    """Validate evidence/ownership only. Never call price, funding or depth APIs."""
    from app import astro_spread_scanner as scanner
    if not isinstance(pair.get('_announcementCard'), dict) or pair.get('status') is not False or pair.get('disableOpen') is not False:
        return False, {'reason': 'announcement_card_must_be_paused'}
    allowed, reason = scanner.astro_spread_pair_submit_guard(pair)
    if not allowed:
        return allowed, reason
    identity = (symbol(pair.get('name')), pair.get('type'), pair.get('buyEx'), pair.get('sellEx'))
    if not any((r['symbol'], r['type'], r['buyExchange'], r['sellExchange']) == identity for r in current_listing_routes()):
        return False, {'reason': 'announcement_route_evidence_unavailable'}
    if scanner.spread_scan_exclude_delisted_exchange_cards():
        try:
            ex = scanner._indexed_delisting_match(scanner._active_delisting_exchange_blocks(), *identity)
        except Exception:
            return False, {'reason': 'delisting_index_unavailable'}
        if ex:
            return False, {'reason': 'contract_delisting_announced', 'exchange': ex}
    return True, {'reason': 'announcement_precreation', 'marketTradabilityRequired': False}


def schedule_listing_cards(config):
    from app import astro_sdk, astro_spread_scanner as scanner
    routes = current_listing_routes()
    pending, display = [], []
    for route in routes:
        # Saved thresholds are configuration defaults, not a measured spread.
        threshold = scanner.spread_scan_ff_min_open_pct() if route['type'] == 'FF' else scanner.spread_scan_sf_route_min_open_pct(route['buyExchange'])
        pair = astro_sdk.build_astro_spread_pair({**route, 'openSpreadPct': threshold}, config)
        pair['_announcementCard'] = {'category': route['category'], 'sources': route['announcements']}
        pair['_chainNote'] = '上架'
        tracked = astro_sdk.astro_route_dedupe_state(pair)
        display.append({**route, 'state': tracked or 'waiting_submission'})
        if tracked is None:
            pending.append(pair)
    # Share the normal serialized submission queue and confirmation/dedupe
    # machinery, with no price revalidator. Do not preempt executable cards.
    result = astro_sdk.schedule_astro_pairs(pending, config, submit_guard=announcement_submit_guard) if pending else {}
    with _lock:
        _state.update(listingRoutes=display, listingScheduleError=None,
                      lastListingScheduleAt=datetime.now(timezone.utc).isoformat(), listingQueueState=result.get('state'))


def ensure_announcement_labels(cards):
    """Backfill confirmed announcement cards, including late submission recovery."""
    from app.astro_card_registry import auto_created_route_records
    from app.astro_sdk import _publish_astro_chain_label, astro_chain_label_publish_enabled
    records = {r.get('astroPairId'): r for r in auto_created_route_records() if r.get('announcementPrecreated') and r.get('astroPairId')}
    attempts = 0
    for card in cards:
        card_id = card.get('id')
        row = records.get(card_id)
        if not row or card_id in _label_published_ids:
            continue
        if any(card.get(k) != row.get(k) for k in ('name', 'type', 'buyEx', 'sellEx')):
            continue
        if attempts >= 4:
            break
        attempts += 1
        note = '上架'
        if astro_chain_label_publish_enabled():
            from app.astro_transfer_labels import collect
            transfer_note, _ = collect(card)
            if transfer_note:
                note += '；' + transfer_note
        if _publish_astro_chain_label({**card, '_chainNote': note}, card):
            _label_published_ids.add(card_id)
    with _lock:
        _state['listingLabelPublishedCount'] = len(_label_published_ids)
