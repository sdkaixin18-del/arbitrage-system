"""User rules: funding boundary, isolated DEX consent and venue switches."""
import json
import pytest
from app import astro_sf_funding as funding
from app import astro_spread_scanner as scanner

@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('ASTRO_SPREAD_SUBSCRIPTIONS_FILE', str(tmp_path/'settings.json'))
    funding._cache.clear()
    funding._inflight.clear()

@pytest.mark.parametrize('spread,rate,eligible,status,calls',[
    (2.499,0,True,'non_negative',1),
    (2.499,-0.00001,False,'negative',1),
    (2.499,None,False,'funding_unavailable',1),
    (2.5,-0.1,True,'spread_exempt',0),
    (3,None,True,'spread_exempt',0),
])
def test_exact_funding_boundary(spread,rate,eligible,status,calls):
    seen=[]
    result=funding.check('SF',spread,'binance','ABC',lambda url,params:seen.append((url,params)) or {'lastFundingRate':rate})
    assert result['eligible'] is eligible and result['status']==status
    assert len(seen)==calls

def test_ff_never_reads_and_exemption_does_not_prime_cache():
    def fail(*a):pytest.fail('must not fetch')
    assert funding.check('FF',1,'gate','ABC',fail)['eligible']
    assert funding.check('SF',3,'gate','ABC',fail)['eligible']
    # Spread falls below exemption at the actual depth check: funding must be read.
    assert not funding.check('SF',2.4,'gate','ABC',lambda *a:{'funding_rate':-0.01})['eligible']

def test_cache_expires_and_is_scoped_to_contract(monkeypatch):
    clock=[10.0];monkeypatch.setattr(funding.time,'monotonic',lambda:clock[0])
    calls=[]
    fetch=lambda *a: calls.append(a) or {'funding_rate':'0.0001'}
    funding.check('SF',1,'gate','ABC',fetch)
    assert funding.check('SF',1,'gate','ABC',fetch)['cached']
    funding.check('SF',1,'gate','XYZ',fetch)
    assert len(calls)==2
    clock[0]+=11
    funding.check('SF',1,'gate','ABC',fetch)
    assert len(calls)==3

@pytest.mark.parametrize('exchange,body',[
    ('binance',{'lastFundingRate':'0.0001'}),('aster',{'lastFundingRate':'0.0001'}),
    ('bybit',{'retCode':0,'result':{'list':[{'fundingRate':'0.0001'}]}}),
    ('bitget',{'code':'00000','data':[{'fundingRate':'0.0001'}]}),
    ('okx',{'code':'0','data':[{'fundingRate':'0.0001'}]}),('gate',{'funding_rate':'0.0001'})])
def test_funding_decimal_to_percent(exchange,body):
    assert funding.parse_rate(exchange,body)==pytest.approx(0.01)

def test_bad_funding_not_zero():
    for value in [None,'','nan','inf','-inf']:
        with pytest.raises((ValueError,TypeError)):funding.parse_rate('binance',{'lastFundingRate':value})

ADDRESS='0x'+'a'*40

def candidate(exchange):
    return {'type':'SF','symbol':'ABC','buyExchange':exchange,'buyMarket':'spot','sellExchange':'gate','sellMarket':'future',
            'openSpreadPct':3,'buyVolume24hUsdt':1e7,'sellVolume24hUsdt':1e7,
            'dexConfig':{'exchange':exchange,'chainIndex':'56','contractAddress':ADDRESS},'dexIdentity':{'status':'verified'}}

def test_confirmation_preserves_other_settings_and_shares_exact_asset_across_dex(monkeypatch):
    scanner.update_astro_spread_subscriptions(['binanceFuture','okxdexSpot','pancakeswapv3Spot'],
        dex_mapped_assets=[],min_volume_usdt=123456,max_notional_usdt=20,blocked_coins=['BLOCK'],sf_pancakeswap_v3_auto_card_enabled=False)
    before=scanner._read_saved_subscription_payload()
    scanner.confirm_astro_dex_mapping({'exchange':'okxdex','symbol':'ABC','chainIndex':'56','contractAddress':ADDRESS})
    after=scanner._read_saved_subscription_payload()
    for key,value in before.items():
        if key not in {'dexMappedAssets','updatedAt'}:assert after[key]==value,key
    assert scanner._dex_mapping_confirmation(candidate('okxdex'))['status']=='confirmed'
    assert scanner._dex_mapping_confirmation(candidate('pancakeswapv3'))['status']=='confirmed'
    scanner.confirm_astro_dex_mapping({'exchange':'pancakeswapv3','symbol':'ABC','chainIndex':'56','contractAddress':ADDRESS})
    assert len(scanner.spread_scan_dex_mapped_assets())==1
    # Same exact confirmation is idempotent.
    scanner.confirm_astro_dex_mapping({'exchange':'pancakeswapv3','symbol':'ABC','chainIndex':'56','contractAddress':ADDRESS})
    assert len(scanner.spread_scan_dex_mapped_assets())==1

@pytest.mark.parametrize('address',['','0xabc','0x'+'g'*40])
def test_invalid_mapping_cannot_be_confirmed(address):
    with pytest.raises(ValueError):scanner.confirm_astro_dex_mapping({'exchange':'pancakeswapv3','symbol':'ABC','chainIndex':'56','contractAddress':address})

def test_pancake_switch_independent_of_okx():
    scanner.update_astro_spread_subscriptions(['okxdexSpot','pancakeswapv3Spot','gateFuture'],
        sf_okxdex_auto_card_enabled=True,sf_pancakeswap_v3_auto_card_enabled=False)
    a={**candidate('okxdex'),'dexMapping':{'status':'confirmed'}}
    b={**candidate('pancakeswapv3'),'dexMapping':{'status':'confirmed'}}
    assert scanner._meets_auto_card_rule(a)
    assert not scanner._meets_auto_card_rule(b)
    scanner.update_astro_spread_subscriptions(['okxdexSpot','pancakeswapv3Spot','gateFuture'],
        sf_okxdex_auto_card_enabled=False,sf_pancakeswap_v3_auto_card_enabled=True)
    assert not scanner._meets_auto_card_rule(a)
    assert scanner._meets_auto_card_rule(b)

def test_pending_same_coin_keeps_both_venues():
    rows=[{'exchange':ex,'symbol':'ABC','chainIndex':'56','contractAddress':ADDRESS,'targetExchanges':['gate']} for ex in ['okxdex','pancakeswapv3']]
    pending=scanner._merge_pending_dex_mapping_items(rows,[])
    assert len(pending)==2
    scanner.confirm_astro_dex_mapping({'exchange':'okxdex','symbol':'ABC','chainIndex':'56','contractAddress':ADDRESS})
    pending=scanner._merge_pending_dex_mapping_items(pending,[])
    assert pending == []

@pytest.mark.parametrize('sell_price,rate,passes',[(102.4,-0.01,False),(102.4,0,True),(102.5,-0.01,False),(102.6,-0.01,True)])
def test_final_depth_gate_enforces_funding(monkeypatch,sell_price,rate,passes):
    import time
    from test_astro_spread_scanner import sdk_config
    scanner.update_astro_spread_subscriptions(['binanceSpot','bitgetFuture'],min_volume_usdt=0,max_notional_usdt=20,sf_min_open_spread_pct=0.8)
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda:{})
    monkeypatch.setattr(scanner,'spread_scan_exclude_delisted_exchange_cards',lambda:False)
    def read(_client,exchange,market,symbol,aliases):
        stamp=int(time.time()*1000);buy=exchange=='binance'
        return {'asks':[[100 if buy else sell_price+1,100]],'bids':[[99 if buy else sell_price,100]],
                'timestamp':stamp,'receivedAt':stamp,'localReceivedAt':stamp,'timestampSource':'exchange_depth_update',
                'bestAsk':100 if buy else sell_price+1,'bestBid':99 if buy else sell_price,'bestAskQuantity':100,'bestBidQuantity':100,
                'exchange':exchange,'market':market,'requestDurationMs':1,'endpoint':'https://exchange.example/depth'}
    monkeypatch.setattr(scanner,'_fetch_direct_depth_book',read)
    calls=[]
    class Response:
        def close(self):pass
        def raise_for_status(self):pass
        def json(self):return {'code':'00000','data':[{'fundingRate':str(rate)}]}
    monkeypatch.setattr(scanner,'_scanner_public_get',lambda *a,**kw:calls.append(kw) or Response())
    from types import SimpleNamespace
    jobs=[]
    monkeypatch.setattr(funding,'_queued',set())
    monkeypatch.setattr(funding,'_prefetch_pool',SimpleNamespace(submit=lambda f:jobs.append(f)))
    route={'name':'ABC','type':'SF','buyEx':'binance','sellEx':'bitget','_buyVolume24hUsdt':1e6,'_sellVolume24hUsdt':1e6}
    pair,report=scanner._fetch_direct_route_once_local(route,sdk_config(),executable_depth_first=True)
    if sell_price < 102.6:
        assert pair is None and report['reason']=='sf_funding_pending'
        jobs.pop()()
        pair,report=scanner._fetch_direct_route_once_local(route,sdk_config(),executable_depth_first=True)
    assert (pair is not None) is passes,report
    assert len(calls)==(0 if sell_price>=102.6 else 1)
    if pair:assert pair['status'] is False and pair['disableOpen'] is False

def test_submit_guard_rechecks_disabled_dex_switch():
    scanner.update_astro_spread_subscriptions(['pancakeswapv3Spot','gateFuture'],sf_pancakeswap_v3_auto_card_enabled=False)
    allowed,report=scanner.astro_spread_pair_submit_guard({'name':'ABC','type':'SF','buyEx':'pancakeswapv3','sellEx':'gate'})
    assert not allowed and report['reason']=='sf_pancakeswapv3_auto_card_paused'
