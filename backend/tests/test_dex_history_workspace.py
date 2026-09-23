"""On-demand history must preserve chain identity and never alter trading state."""
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import HTTPException
from app import dex_history as h

ADDRESS = '0x' + 'a' * 40

def coin(symbol='哈基米', chain='56', address=ADDRESS):
    return {'name':symbol,'chainIndex':chain,'contractAddress':address,'secret':'must-not-return'}

def card(symbol='哈基米', buy='okxdex', sell='binance'):
    return {'id':'real-card','type':'SF','name':symbol,'buyEx':buy,'sellEx':sell,'openPosition':'.012','closePosition':'-.002','secret':'must-not-return'}

@pytest.fixture(autouse=True)
def clear_cache():
    h._cache.clear(); h._inflight.clear()
    yield
    h._cache.clear(); h._inflight.clear()

def test_six_chains_and_unicode_symbols():
    assert h.normalize_futures_symbol('哈基米', 'gate') == ('哈基米','哈基米_USDT')
    assert h.normalize_dex_network('Robinhood Chain') == 'robinhood'
    assert h.CHAIN_NETWORKS['4663'] == 'robinhood'
    assert len(h.CHAIN_NETWORKS) == 6
    with pytest.raises(ValueError): h.normalize_futures_symbol('<script>', 'bn')

def test_shared_asset_catalog_and_aliases_are_read_only():
    mappings=[{'symbol':'哈基米','chainIndex':'56','contractAddress':ADDRESS,'exchange':v} for v in ['okxdex','pancakeswapv3']]
    result=h.build_route_catalog([card(),card(buy='pancakeswapv3')],[coin()],mappings,[],{('binance','future','1000哈基米'):('哈基米',1000)})
    assert len(result['assets']) == 1
    assert result['cards'][0]['assetIds'] == result['cards'][1]['assetIds']
    assert result['cards'][0]['futuresSymbol'] == '1000哈基米'
    assert result['cards'][0]['futuresDivisor'] == 1000
    assert result['cards'][0]['openTargetPct'] == 1.2
    assert 'secret' not in str(result)
    assert mappings[0]['exchange'] == 'okxdex'

def test_same_address_different_chain_is_never_silently_selected():
    result=h.build_route_catalog([card()],[coin(),coin(chain='1')],[],[],{})
    assert len(result['assets']) == 2
    assert result['cards'][0]['resolution'] == 'ambiguous'
    assert len(result['cards'][0]['assetIds']) == 2

def test_exact_registered_card_can_disambiguate_and_solana_case_is_preserved():
    address='HHNPd8DpneXbaYBiGC7SU28mYxocbkMznPABPN3sGTyU'
    result=h.build_route_catalog([card()],[coin(chain='501',address=address)],[],[],{})
    assert result['assets'][0]['contractAddress'] == address
    result=h.build_route_catalog([card()],[coin(),coin(chain='1')],[],[{'astroPairId':'real-card','dexChainIndex':'1','dexContractAddress':ADDRESS}],{})
    assert result['cards'][0]['assetIds'] == ['1:'+ADDRESS]

def test_missing_asset_and_non_dex_sf_are_not_fabricated():
    result=h.build_route_catalog([card(),{**card(),'type':'FF'},card(buy='binance')],[],[],[],{})
    assert len(result['cards']) == 1
    assert result['cards'][0]['resolution'] == 'missing'
    assert result['cards'][0]['assetIds'] == []

def test_completed_candles_and_per_coin_price_conversion(monkeypatch):
    monkeypatch.setattr(h.time,'time',lambda: 600.5)
    monkeypatch.setattr(h,'_fetch_dex_pool_metadata',lambda *a:{'poolName':'哈基米 / USDT','tokenSide':'base','poolDex':'pancakeswap-v3-bsc'})
    reads=[]
    def dex(*args):
        reads.append(('dex',args[3]));return {480000:1.0,540000:1.0,600000:100.0}
    def futures(*args):
        reads.append(('futures',args[3]));return {480000:1000.0,540000:1000.0,600000:1.0}
    monkeypatch.setattr(h,'_fetch_dex_rows',dex);monkeypatch.setattr(h,'_fetch_binance_rows',futures)
    result=h._read_spread_history(ADDRESS,'哈基米','bsc','bn','1000哈基米',4,1000)
    assert [p['timestamp'] for p in result['points']] == [480000,540000]
    assert all(p['spreadPct']==0 for p in result['points'])
    assert all(end == 599999 for _,end in reads)
    assert 'pancakeswap-v3-bsc' in result['sources']['dex']

def test_concurrent_identical_queries_share_work_and_errors_can_retry():
    started=threading.Event();release=threading.Event();calls=[]
    def loader():
        calls.append(1);started.set();assert release.wait(2);return {'value':1}
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(h._cached_read,('same',),loader)
        assert started.wait(2)
        second=pool.submit(h._cached_read,('same',),loader)
        release.set();assert first.result() == second.result()
    assert len(calls)==1
    assert h._cached_read(('same',),lambda:pytest.fail('cache miss')) == {'value':1}
    with pytest.raises(ValueError): h._cached_read(('failed',),lambda:(_ for _ in ()).throw(ValueError('failed')))
    assert h._cached_read(('failed',),lambda:2)==2

def test_cache_bound_and_different_chains_not_merged():
    assert h._cached_read(('history','bsc',ADDRESS),lambda:1) == 1
    assert h._cached_read(('history','eth',ADDRESS),lambda:2) == 2
    for i in range(80):h._cached_read(('test',i),lambda:i)
    assert len(h._cache)==64

def test_route_reads_degrade_without_exposing_error_or_writing(monkeypatch):
    from app import astro_spread_scanner as scanner
    from app import astro_card_registry as registry
    monkeypatch.setattr(h,'_read_astro_cards',lambda:(_ for _ in ()).throw(RuntimeError('secret-url')))
    monkeypatch.setattr(h,'_read_astro_coins',lambda:[coin()])
    monkeypatch.setattr(scanner,'spread_scan_dex_mapped_assets',lambda:[])
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda:{})
    monkeypatch.setattr(registry,'auto_created_route_records',lambda:[])
    result=h._read_route_catalog()
    assert len(result['warnings']) == 1 and len(result['assets']) == 1
    assert 'secret-url' not in str(result)


def test_launchpad_token_address_resolves_to_migrated_pool(monkeypatch):
    monkeypatch.setattr(h.time,'time',lambda:600.5)
    monkeypatch.setattr(h,'_fetch_dex_pool_metadata',lambda *a:{'inputAddressRole':'token','poolName':'旧发射池','tokenSide':'base','poolDex':'four-meme'})
    pool='0x'+'b'*40
    monkeypatch.setattr(h,'_fetch_best_token_pool',lambda *a:(pool,{'poolName':'已迁移主池','tokenSide':'base','poolDex':'pancakeswap_v2'}))
    def dex(client,pair,*args):
        assert pair['poolAddress']==pool
        assert pair['inputAddress']==ADDRESS
        return {480000:1,540000:1}
    monkeypatch.setattr(h,'_fetch_dex_rows',dex)
    monkeypatch.setattr(h,'_fetch_gate_rows',lambda *a:{480000:1,540000:1})
    result=h._read_spread_history(ADDRESS,'哈基米','bsc','gt','哈基米',4)
    assert result['pair']['queryAddressType']=='token'
    assert result['pair']['poolDex']=='pancakeswap_v2'
