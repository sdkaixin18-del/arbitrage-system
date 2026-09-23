import threading
from types import SimpleNamespace
import pytest
from app import astro_sf_funding as funding, astro_spread_scanner as scanner
from test_astro_spread_scanner import sdk_config

@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    now=[100.0]
    clock=SimpleNamespace(monotonic=lambda:now[0],time=lambda:now[0]+1800000000)
    monkeypatch.setattr(funding,'time',clock)
    monkeypatch.setattr(scanner,'time',clock)
    monkeypatch.setattr(funding,'_cache',{})
    monkeypatch.setattr(funding,'_inflight',set())
    monkeypatch.setattr(funding,'_pulse_negative_since',{})
    monkeypatch.setattr(scanner,'_hot_routes',{})
    monkeypatch.setattr(scanner,'_prefetch_hot_funding',lambda item:None)
    monkeypatch.setattr(scanner,'_state',{})
    monkeypatch.setattr(scanner,'_stop',threading.Event())
    monkeypatch.setattr(scanner,'_hot_pre_api_filter',lambda p:None)
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda:{})
    monkeypatch.setattr(scanner,'_append_decision_audit',lambda *a,**kw:None)
    monkeypatch.setenv('ASTRO_SPREAD_SUBSCRIPTIONS_FILE',str(tmp_path/'settings.json'))
    return now

def item(spread=1.0,buy='binance',kind='SF',rate='0.010'):
    candidate={'symbol':'ABC','type':kind,'buyExchange':buy,'sellExchange':'bitget',
               'buyMarket':'spot' if kind=='SF' else 'future','sellMarket':'future',
               'openSpreadPct':spread,'quoteAt':100,'buyVolume24hUsdt':1e6,'sellVolume24hUsdt':1e6,
               'sellPulseFunding':hint(rate)}
    return {'identity':('ABC',kind,buy,'bitget','',''),'pair':{'name':'ABC','type':kind,'buyEx':buy,'sellEx':'bitget'},
            'candidate':candidate,'reasons':['pulse_above_threshold']}

def response(rate):return {'code':'00000','data':[{'fundingRate':rate}]}

def hint(rate='-0.010',**kw):
    return {'rawRate':rate,'rawSymbol':'ABC','marketAtMs':int(funding.time.time()*1000),**kw}

@pytest.mark.parametrize('spread,rate,depth',[(1,'-0.01',False),(1,'0',True),(2.299,'-0.01',False),(2.3,'-0.01',True),(3,None,True),(1,'-0.000',True),(1,None,True)])
def test_pulse_precedes_depth_without_api(monkeypatch,spread,rate,depth):
    events=[]
    monkeypatch.setattr(scanner,'_fetch_sf_funding',lambda *a:pytest.fail('initial filter must not request API'))
    monkeypatch.setattr(scanner,'_fetch_direct_route_once',lambda *a,**kw:(events.append('depth') or None,{'reason':'below_threshold_or_rule_failed'}))
    _,pair,report=scanner._hot_route_direct_check(item(spread,rate=rate),sdk_config())
    assert ('depth' in events) is depth
    assert events==(['depth'] if depth else [])
    if not depth:assert report['depthSkipped'] and pair is None


def test_negative_rate_shared_by_routes_and_stops_depth(monkeypatch,isolated):
    seen=[]
    monkeypatch.setattr(scanner,'_fetch_sf_funding',lambda *a:seen.append(a) or response(-0.01))
    monkeypatch.setattr(scanner,'_fetch_direct_route_once',lambda *a,**kw:pytest.fail('negative rate must skip depth'))
    # An actual earlier final check primes the shared 10-second API cache.
    funding.check('SF',1,'bitget','ABC',scanner._fetch_sf_funding)
    for buy in ['binance','gate']:
        route=item(buy=buy);scanner._hot_routes[route['identity']]=route
        identity,pair,report=scanner._hot_route_direct_check(route,sdk_config())
        scanner._process_hot_direct_result(identity,pair,report,sdk_config())
        assert route['nextPollMonotonic']==pytest.approx(110)
        assert scanner._hot_route_wait_snapshot()['routeWaits'][0]['fundingWaiting']
    assert len(seen)==1
    assert scanner._state['hotMonitor']['fundingDepthSkippedCount']==2
    isolated[0]=110
    monkeypatch.setattr(scanner,'_fetch_sf_funding',lambda *a:seen.append(a) or response(0))
    assert scanner._hot_funding_precheck(item())['eligible']
    assert len(seen)==1  # Cache expiry does not cause a prefilter API request.
    funding.check('SF',1,'bitget','ABC',scanner._fetch_sf_funding)
    assert len(seen)==2


def test_error_backoff_shared_and_success_resets(monkeypatch,isolated):
    calls=[]
    def fail(*a):calls.append(a);raise RuntimeError('temporary')
    for expected in [1,3,10,30]:
        result=funding.check('SF',1,'bitget','ABC',fail)
        assert result['retryAfterSeconds']==expected
        count=len(calls)
        again=funding.precheck('SF',1,'bitget','ABC')
        assert again['cached'] and not again['read'] and len(calls)==count
        # Warning band never waits on a funding outage, but final gate stays strict.
        assert funding.precheck('SF',2.3,'bitget','ABC')['eligible']
        assert not funding.check('SF',2.49,'bitget','ABC',fail)['eligible']
        assert funding.check('SF',2.5,'bitget','ABC',fail)['eligible']
        isolated[0]+=expected
    assert funding.check('SF',1,'bitget','ABC',lambda *a:response(0))['eligible']
    isolated[0]+=10
    assert funding.check('SF',1,'bitget','ABC',fail)['retryAfterSeconds']==1


def test_single_flight_does_not_duplicate_or_block_warning_band():
    started=threading.Event();release=threading.Event();calls=[]
    def fetch(*a):
        calls.append(a);started.set();assert release.wait(2);return response(-0.01)
    thread=threading.Thread(target=lambda:funding.check('SF',1,'bitget','ABC',fetch));thread.start()
    try:
        assert started.wait(2)
        assert funding.precheck('SF',1,'bitget','ABC')['status']=='funding_pending'
        assert funding.precheck('SF',2.3,'bitget','ABC')['eligible']
        assert len(calls)==1
    finally:release.set();thread.join(2)


def test_crossing_23_wakes_without_point_one_improvement(monkeypatch,isolated):
    monkeypatch.setattr(scanner,'_meets_auto_card_rule',lambda c:True)
    monkeypatch.setattr(scanner,'astro_route_dedupe_state',lambda p:None)
    monkeypatch.setattr(scanner,'build_astro_spread_pairs',lambda c,conf:[item()['pair']])
    c=item(2.29)['candidate']
    scanner._register_hot_candidates([c],set(),sdk_config())
    identity=next(iter(scanner._hot_routes))
    scanner._hot_routes[identity].update(fundingWaitUntilMonotonic=110,nextPollMonotonic=110)
    isolated[0]=101
    scanner._register_hot_candidates([{**c,'quoteAt':101}],set(),sdk_config())
    assert scanner._hot_routes[identity]['nextPollMonotonic']==110
    scanner._register_hot_candidates([{**c,'quoteAt':102,'openSpreadPct':2.3}],set(),sdk_config())
    assert scanner._hot_routes[identity]['nextPollMonotonic']==101
    assert scanner._hot_routes[identity]['fundingWaitUntilMonotonic'] is None


def test_new_pulse_during_request_cannot_be_overwritten_by_funding_wait():
    route=item(2.3)
    scanner._update_hot_price_cadence(route,{'fundingPrecheck':{'eligible':False,'retryAfterSeconds':10}})
    assert route['nextPollMonotonic']==100
    assert route['fundingWaitUntilMonotonic'] is None


def test_ff_and_unknown_listing_keep_depth_without_funding(monkeypatch):
    monkeypatch.setattr(scanner,'_fetch_sf_funding',lambda *a:pytest.fail('must not read'))
    route=item(kind='FF');assert scanner._hot_funding_precheck(route)['eligible']
    route=item();route['candidate'].pop('openSpreadPct')
    assert scanner._hot_funding_precheck(route)['eligible']


def test_price_only_backoff_unchanged():
    route=item();route['lastDirectCheckStartedMonotonic']=100
    for delay in [.5,2,4,5]:
        scanner._update_hot_price_cadence(route,{'reason':'below_threshold_or_rule_failed','latestOpenSpreadPct':0.1})
        assert route['pollIntervalMs']==delay*1000


@pytest.mark.parametrize('value',[None,'','--','NaN','inf','-inf','-0.000','0.000','-0.001','0.010'])
def test_ambiguous_pulse_can_only_pass_depth_not_final_gate(value):
    pre=funding.precheck('SF',1,'bitget','ABC',pulse=hint(value))
    assert pre['eligible'] and not pre['read'] and pre['finalApiRequired']
    if value=='-0.000':assert pre['pulseNegativeSign']
    calls=[]
    result=funding.check('SF',1,'bitget','ABC',lambda *a:calls.append(a) or response('-0.000001'))
    assert not result['eligible'] and len(calls)==1


@pytest.mark.parametrize('change',[{'marketAtMs':0},{'marketAtMs':1800000200000},{'rawSymbol':'1000ABC'}])
def test_stale_future_or_wrong_contract_pulse_cannot_block(change):
    result=funding.precheck('SF',1,'bitget','ABC',pulse=hint(**change))
    assert result['eligible'] and result['status']=='pulse_unknown'


def test_pulse_negative_wait_is_bounded_across_routes_and_updates(isolated):
    for elapsed in [0,2,9,10,20]:
        isolated[0]=100+elapsed
        result=funding.precheck('SF',1,'bitget','ABC',pulse=hint('-0.010'))
        assert result['eligible'] is (elapsed>=10)
        assert not result['read']
        if elapsed<10:assert result['retryAfterSeconds']==10-elapsed
    # Precise positive cache wins even when Pulse is still negative.
    funding.check('SF',1,'bitget','ABC',lambda *a:response('0.00001'))
    result=funding.precheck('SF',1,'bitget','ABC',pulse=hint())
    assert result['eligible'] and result['cached'] and result['source']=='exchange_api'


def test_changed_pulse_rate_wakes_wait_and_old_result_cannot_restore_wait(monkeypatch,isolated):
    monkeypatch.setattr(scanner,'_meets_auto_card_rule',lambda c:True)
    monkeypatch.setattr(scanner,'astro_route_dedupe_state',lambda p:None)
    monkeypatch.setattr(scanner,'build_astro_spread_pairs',lambda c,conf:[item()['pair']])
    c=item(rate='-0.010')['candidate']
    scanner._register_hot_candidates([c],set(),sdk_config())
    identity=next(iter(scanner._hot_routes))
    old=scanner._hot_funding_precheck(scanner._hot_routes[identity])
    scanner._hot_routes[identity].update(fundingWaitUntilMonotonic=110,nextPollMonotonic=110)
    isolated[0]=101
    scanner._register_hot_candidates([{**c,'quoteAt':101,'sellPulseFunding':hint('0.010')}],set(),sdk_config())
    route=scanner._hot_routes[identity]
    assert route['nextPollMonotonic']==101 and route['fundingWaitUntilMonotonic'] is None
    scanner._update_hot_price_cadence(route,{'fundingPrecheck':old})
    assert route['nextPollMonotonic']==101 and route['fundingWaitUntilMonotonic'] is None


def test_hint_cache_capacity_fails_open_instead_of_restarting_waits():
    funding._pulse_negative_since.update({('gate',str(i)):100 for i in range(512)})
    result=funding.precheck('SF',1,'bitget','ABC',pulse=hint())
    assert result['eligible'] and result['status']=='pulse_hint_capacity'
    assert len(funding._pulse_negative_since)==512


def test_pulse_parser_preserves_raw_rate_and_mapped_contract(monkeypatch):
    scanner.update_astro_spread_subscriptions(['binanceSpot','bybitFuture'],min_volume_usdt=0,sf_min_open_spread_pct=0.8)
    stamp=int(funding.time.time()*1000)
    body={'data':{
        'binanceSpot':{'ts':stamp,'list':[{'name':'BTTUSDT','a':1,'b':0.99,'trade24Count':1e6}]},
        'bybitFuture':{'ts':stamp,'list':[{'name':'1000BTTUSDT','a':1020,'b':1010,'rate':'-0.000','trade24Count':1e6}]}}}
    aliases={('bybit','future','1000BTT'):('BTT',1000)}
    candidates,_=scanner.scan_pulse_spreads([body],now_ms=stamp,symbol_aliases=aliases)
    c=next(c for c in candidates if c['type']=='SF')
    assert c['sellPulseFunding']=={'rawRate':'-0.000','rawSymbol':'1000BTT','marketAtMs':stamp}
    assert c['sellFundingRatePct'] is None
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda:aliases)
    pre=scanner._hot_funding_precheck({'pair':{'type':'SF','name':'BTT','sellEx':'bybit'},'candidate':c})
    assert pre['eligible'] and pre['pulseNegativeSign'] and pre['finalApiRequired']


@pytest.mark.parametrize('sell_price,rate,api_calls,passes',[(100.1,'0.0001',0,False),(101,'-0.000001',1,False),(101,'0',1,True),(103,'-0.01',0,True)])
def test_full_hot_path_only_reads_api_after_executable_threshold(monkeypatch,sell_price,rate,api_calls,passes):
    scanner.update_astro_spread_subscriptions(['binanceSpot','bitgetFuture'],min_volume_usdt=0,max_notional_usdt=20,sf_min_open_spread_pct=0.8)
    monkeypatch.setattr(scanner,'spread_scan_exclude_delisted_exchange_cards',lambda:False)
    events=[]
    def book(_client,exchange,market,symbol,aliases):
        events.append('book');stamp=int(funding.time.time()*1000);buy=exchange=='binance'
        ask=100 if buy else sell_price+.1;bid=99.9 if buy else sell_price
        return {'asks':[[ask,100]],'bids':[[bid,100]],'timestamp':stamp,'receivedAt':stamp,'localReceivedAt':stamp,
                'timestampSource':'exchange_depth_update','bestAsk':ask,'bestBid':bid,'bestAskQuantity':100,'bestBidQuantity':100,
                'exchange':exchange,'market':market,'requestDurationMs':1,'endpoint':'https://exchange.example/depth'}
    monkeypatch.setattr(scanner,'_fetch_direct_depth_book',book)
    monkeypatch.setattr(scanner,'_fetch_sf_funding',lambda *a:events.append('funding') or response(rate))
    monkeypatch.setattr(scanner,'_fetch_direct_route_once',scanner._fetch_direct_route_once_local)
    jobs=[]
    monkeypatch.setattr(funding, '_queued', set())
    monkeypatch.setattr(funding, '_prefetch_pool', SimpleNamespace(submit=lambda f: jobs.append(f)))
    route=item(rate='-0.000')
    _,pair,report=scanner._hot_route_direct_check(route,sdk_config())
    if api_calls:
        assert pair is None and report['reason']=='sf_funding_pending'
        assert len(jobs)==1
        jobs.pop()()
        _,pair,report=scanner._hot_route_direct_check(route,sdk_config())
    assert (pair is not None) is passes,report
    assert events.count('funding')==api_calls
    if api_calls:assert events.index('funding')>=2
    if pair:
        assert pair['status'] is False and pair['disableOpen'] is False
        # The second independent book pass must run, but reuse exact funding.
        scanner._hot_route_direct_check(route,sdk_config())
        assert events.count('book')>=4 and events.count('funding')==api_calls


def test_prefetch_shared_with_final_gate(monkeypatch):
    started=threading.Event();release=threading.Event();done=threading.Event();calls=[]
    def fetch(*args):
        calls.append(args);started.set();assert release.wait(2);return response(0)
    class Pool:
        def submit(self, fn):
            def run():
                try:fn()
                finally:done.set()
            threading.Thread(target=run).start()
    monkeypatch.setattr(funding,'_prefetch_pool',Pool())
    monkeypatch.setattr(funding,'_queued',set())
    assert funding.prefetch('bitget','ABC',fetch)
    try:
        assert started.wait(2)
        assert not funding.prefetch('bitget','ABC',fetch)
        assert funding.check('SF',1,'bitget','ABC',fetch)['status']=='funding_pending'
        assert funding.check('SF',2.5,'bitget','ABC',fetch)['eligible']
    finally:
        release.set();assert done.wait(2)
    assert funding.check('SF',1,'bitget','ABC',fetch)['eligible']
    assert len(calls)==1


def test_final_funding_failure_controls_retry(isolated):
    route=item()
    scanner._update_hot_price_cadence(route,{'sfFundingCheck':{'eligible':False,'retryAfterSeconds':1},
                                          'fundingPrecheck':{'eligible':True}})
    assert route['nextPollMonotonic']==101


def test_negative_is_not_missed_opportunity():
    assert not scanner._append_possible_missed_opportunity(('ABC','SF','gate','bitget','',''),decision='sf_negative',details={})


def test_funding_inflight_is_not_a_missed_opportunity(monkeypatch):
    monkeypatch.setattr(scanner,'astro_route_dedupe_state',lambda identity:pytest.fail('pending must return before missed-opportunity checks'))
    assert not scanner._append_possible_missed_opportunity(('ABC','SF','gate','bitget','',''),decision='sf_funding_pending',details={})
