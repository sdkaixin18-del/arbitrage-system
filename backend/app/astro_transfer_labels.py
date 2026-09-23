"""Optional transfer annotations: bounded prefetch, no trading/creation gate."""
from concurrent.futures import ThreadPoolExecutor, wait
import json
import re
import threading
import time

import httpx

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='astro-transfer')
_LOCK = threading.RLock()
_CACHE = {}
_PENDING = {}
_CLIENT = httpx.Client(timeout=1.5, headers={'User-Agent': 'Mozilla/5.0'},
                     limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
_NAMES = {'gate':'Gate','bitget':'Bitget','okx':'OKX','bybit':'Bybit','binance':'Binance',
          'kucoin':'KuCoin','mexc':'MEXC'}
TTL = 60
MAX_PENDING = 16


def flag(value):
    if isinstance(value, bool):
        return value
    if value in ('true', '1', 1):
        return True
    if value in ('false', '0', 0):
        return False
    return None


def invert(value):
    parsed = flag(value)
    return None if parsed is None else not parsed


def normalize(ex, coin, payload):
    """Keep missing fields unknown; require an exact asset match."""
    rows = []
    if ex == 'gate':
        if payload.get('currency') != coin:
            return []
        for r in payload.get('chains') or []:
            rows.append({'chain':r.get('chain') or r.get('name'),
                         'deposit':invert(r.get('deposit_disabled')),
                         'withdraw':invert(r.get('withdraw_disabled'))})
    elif ex == 'bitget':
        if payload.get('code') != '00000':
            return []
        for asset in payload.get('data') or []:
            if asset.get('coin') != coin:
                continue
            for r in asset.get('chains') or []:
                rows.append({'chain':r.get('chain'), 'deposit':flag(r.get('rechargeable')),
                             'withdraw':flag(r.get('withdrawable'))})
    elif ex in {'okx', 'bybit', 'mexc'}:
        if payload.get('code') != 0:
            return []
        for asset in payload.get('data') or []:
            if asset.get('ccy' if ex == 'okx' else 'coin') != coin:
                continue
            chains = [asset] if ex == 'okx' else asset.get('chains' if ex == 'bybit' else 'networkList') or []
            for r in chains:
                dep, wd = ('canDep','canWd') if ex == 'okx' else ('chainDeposit','chainWithdraw') if ex == 'bybit' else ('depositEnable','withdrawEnable')
                rows.append({'chain':r.get('chain') or r.get('network') or r.get('chainType'),
                             'deposit':flag(r.get(dep)), 'withdraw':flag(r.get(wd))})
    elif ex == 'kucoin':
        asset = payload.get('data') or {}
        if payload.get('code') != '200000' or asset.get('currency') != coin:
            return []
        rows = [{'chain':r.get('chainName') or r.get('chainId'), 'deposit':flag(r.get('isDepositEnabled')),
                 'withdraw':flag(r.get('isWithdrawEnabled'))} for r in asset.get('chains') or []]
    elif ex == 'binance':
        def find(value):
            if isinstance(value, dict):
                if isinstance(value.get('depositWithdrawStatus'), list):
                    return value['depositWithdrawStatus']
                for child in value.values():
                    found = find(child)
                    if found is not None:
                        return found
            return None
        for asset in find(payload) or []:
            if asset.get('coin') == coin:
                rows = [{'chain':r.get('networkDisplay') or r.get('network'),
                         'deposit':flag(r.get('depositEnable')), 'withdraw':flag(r.get('withdrawEnable'))}
                        for r in asset.get('networkList') or []]
    return rows


def _fetch(key):
    ex, coin = key
    started = time.monotonic()
    try:
        if ex in {'okx','bybit','mexc'}:
            r = _CLIENT.post(f'https://utils.astro-btc.xyz:8443/api/query/{ex}CoinQuery', json={'coin':coin})
        else:
            urls = {'gate':f'https://api.gateio.ws/api/v4/spot/currencies/{coin}',
                    'bitget':f'https://api.bitget.com/api/v2/spot/public/coins?coin={coin}',
                    'kucoin':f'https://api.kucoin.com/api/v3/currencies/{coin}',
                    'binance':'https://www.binance.info/zh-CN/network'}
            r = _CLIENT.get(urls[ex])
        r.raise_for_status()
        if ex == 'binance':
            match = re.search(r'<script[^>]*id=["\']__APP_DATA["\'][^>]*>(.*?)</script>',r.text,re.S)
            payload = json.loads(match.group(1)) if match else {}
        else:
            payload = r.json()
        rows = normalize(ex, coin, payload)
        result = {'status':'ok' if rows else 'unknown', 'chains':rows,
                  'source':'astro_utils' if ex in {'okx','bybit','mexc'} else 'exchange_public'}
    except Exception as exc:
        result = {'status':'unknown','chains':[], 'errorType':type(exc).__name__}
    result.update(exchange=ex, coin=coin, checkedAt=time.time(), durationMs=round((time.monotonic()-started)*1000,1))
    with _LOCK:
        _CACHE[key] = (time.monotonic(), result)
        if len(_CACHE) > 512:
            for old in sorted(_CACHE, key=lambda k:_CACHE[k][0])[:128]:
                del _CACHE[old]
    return result


def routes(pair):
    # PancakeSwap cards retain the user's explicit no-remark policy.
    if pair.get('buyEx') == 'pancakeswapv3' or str(pair.get('type')).upper() not in {'SF','FF'}:
        return []
    coin = str(pair.get('name') or '').upper()
    if not re.fullmatch(r'[\w.-]{1,50}',coin):
        return []
    from app.astro_spread_scanner import _load_pulse_symbol_aliases
    aliases = _load_pulse_symbol_aliases()
    result = []
    for ex in dict.fromkeys([pair.get('buyEx'),pair.get('sellEx')]):
        if ex not in _NAMES:
            continue
        names = {raw for (exchange, market, raw), (name, _) in aliases.items()
                 if exchange == ex and market == 'spot' and name == coin}
        if len(names) > 1:
            continue
        raw = next(iter(names),coin)
        if re.fullmatch(r'[\w.-]{1,50}',raw):
            result.append((ex, raw))
    return result


def prefetch(pair):
    with _LOCK:
        for key in routes(pair):
            cached = _CACHE.get(key)
            if cached and time.monotonic()-cached[0] < (TTL if cached[1]['status']=='ok' else 10):
                continue
            if key in _PENDING or len(_PENDING) >= MAX_PENDING:
                continue
            future = _POOL.submit(_fetch,key)
            _PENDING[key] = future
            def done(_, key=key):
                with _LOCK:
                    _PENDING.pop(key,None)
            future.add_done_callback(done)


def label(ex, rows):
    if not rows:
        return ''
    def closed(field):
        return all(r.get(field) is False for r in rows)
    dep, wd = closed('deposit'), closed('withdraw')
    if dep and wd:
        return _NAMES[ex]+'单机币'
    if dep or wd:
        return _NAMES[ex]+('充关' if dep else '提关')
    # A partially closed network must not label the entire exchange closed.
    parts = []
    for r in rows:
        d, w = r.get('deposit') is False, r.get('withdraw') is False
        if (d or w) and r.get('chain'):
            parts.append(str(r['chain'])+('充提关' if d and w else '充关' if d else '提关'))
    return _NAMES[ex]+'('+ '、'.join(parts)+')' if parts else ''


def collect(pair, timeout=5):
    prefetch(pair)
    keys = routes(pair)
    with _LOCK:
        futures = [_PENDING[key] for key in keys if key in _PENDING]
    if futures:
        wait(futures, timeout=timeout)
    notes, evidence = [], []
    with _LOCK:
        for key in keys:
            cached = _CACHE.get(key)
            if not cached or time.monotonic()-cached[0] >= TTL:
                continue
            result = cached[1]
            evidence.append(result)
            if result['status']=='ok':
                note = label(key[0],result['chains'])
                if note:
                    notes.append(note)
    return '；'.join(notes), evidence
