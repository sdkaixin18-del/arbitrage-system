import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from app.astro_sdk_reads import ListReads, ReadDeferred
from app import astro_sf_funding as funding, astro_sdk as sdk
from test_astro_sdk import cleanup_fixture


def test_list_overlap_is_shared_but_later_read_is_fresh():
    reads=ListReads(); entered=threading.Event(); release=threading.Event(); calls=[]
    def fetch():
        calls.append(1);entered.set();assert release.wait(2);return [{'value':1}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(reads.read,'account',fetch,time.monotonic()+3)
        assert entered.wait(1)
        two=pool.submit(reads.read,'account',fetch,time.monotonic()+3)
        deadline=time.monotonic()+1
        while reads.snapshot()['joined']!=1 and time.monotonic()<deadline:time.sleep(.001)
        release.set();a=one.result();b=two.result()
    assert len(calls)==1 and a==b
    a[0]['value']=2;assert b[0]['value']==1
    reads.read('account',fetch,time.monotonic()+1)
    assert len(calls)==2


def test_list_failure_backoff_is_account_scoped_and_resets(monkeypatch):
    from app import astro_sdk_reads as module
    now=[100.];monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:now[0]))
    reads=ListReads()
    def fail():raise ValueError('unavailable')
    for delay in [1,3,10,10]:
        with pytest.raises(ValueError):reads.read('a',fail,200)
        with pytest.raises(ReadDeferred):reads.read('a',fail,200)
        assert reads.read('b',lambda:[],200)==[]
        now[0]+=delay
    assert reads.read('a',lambda:[],200)==[]
    with pytest.raises(ValueError):reads.read('a',fail,200)
    assert reads.failures['a'][1]==now[0]+1


def test_funding_background_is_bounded_shared_and_never_passes_unknown(monkeypatch):
    jobs=[];calls=[]
    monkeypatch.setattr(funding,'_cache',{});monkeypatch.setattr(funding,'_inflight',set())
    monkeypatch.setattr(funding,'_queued',set())
    monkeypatch.setattr(funding,'_prefetch_pool',SimpleNamespace(submit=lambda f:jobs.append(f)))
    fetch=lambda *args:calls.append(args) or {'lastFundingRate':'0.001'}
    for _ in range(10):
        result=funding.check('SF',1,'binance','ABC',fetch,background=True)
        assert not result['eligible'] and result['status']=='funding_pending'
    assert len(jobs)==1 and not calls
    jobs.pop()()
    assert funding.check('SF',1,'binance','ABC',fetch,background=True)['eligible']
    assert len(calls)==1
    for n in range(20):funding.check('SF',1,'binance',str(n),fetch,background=True)
    assert len(jobs)==16
    assert funding.check('SF',2.5,'binance','EXEMPT',fetch,background=True)['eligible']


def test_readable_cleanup_setting_recovers_but_changed_value_stays_protected():
    pair, record=cleanup_fixture()
    record['submittedOnlyConfigFields']=['priceAlertOnlyRise']
    assert sdk._cleanup_safety_check(record,pair)[0]
    pair['priceAlertOnlyRise']=not record['createdPair']['priceAlertOnlyRise']
    assert sdk._cleanup_safety_check(record,pair)==(False,'card_config_changed:priceAlertOnlyRise')


def test_replacement_id_finishes_old_cleanup_without_adopting(monkeypatch,tmp_path):
    from datetime import datetime, timezone, timedelta
    from app import astro_card_registry as reg
    monkeypatch.setenv('ASTRO_AUTO_CARD_REGISTRY_FILE',str(tmp_path/'registry.json'))
    reg.reset_registry_for_tests()
    pair,_=cleanup_fixture();start=datetime.now(timezone.utc)
    reg.register_auto_created_pair(pair,astro_pair=pair,now=start)
    identity=reg.pair_identity(pair);obs={identity:{'state':'invalid','reason':'below_threshold'}}
    replacement={**pair,'id':'user-replacement'}
    assert reg.observe_auto_card_cleanup(obs,[replacement],now=start+timedelta(seconds=600),grace_seconds=0,invalid_seconds=0)==[]
    record=next(iter(reg._load_registry()['routes'].values()))
    assert record['astroPairId']==pair['id'] and record['cleanupSupersededById']=='user-replacement'
    assert record['invalidObservedAt'] is None


def test_funding_worker_closes_clients_after_success_and_failure(monkeypatch):
    import httpx
    from app import astro_spread_scanner as scanner, crypto
    clients=[]
    def get(client,*args,**kwargs):
        assert getattr(crypto._api_priority_local,'level')=='astro'
        assert 5 < crypto.api_request_remaining_seconds() <= 6
        clients.append(client)
        if len(clients) % 3 == 0:
            raise httpx.PoolTimeout('simulated pool failure')
        return httpx.Response(200,json={'ok':True},request=httpx.Request('GET','https://example.com'))
    monkeypatch.setattr(scanner,'_scanner_public_get',get)
    # Repeated failures and recovery, not just two warm successful reads.
    for n in range(30):
        if (n+1) % 3 == 0:
            with pytest.raises(httpx.PoolTimeout):scanner._fetch_sf_funding('https://example.com',{})
        else:
            assert scanner._fetch_sf_funding('https://example.com',{})=={'ok':True}
        assert clients[-1].is_closed
    assert len({id(c) for c in clients})==30


def test_funding_completion_cannot_approve_old_spread(monkeypatch,tmp_path):
    from app import astro_spread_scanner as scanner
    from test_astro_spread_scanner import sdk_config
    monkeypatch.setenv('ASTRO_SPREAD_SUBSCRIPTIONS_FILE',str(tmp_path/'missing.json'))
    monkeypatch.setenv('ASTRO_SPREAD_SF_MIN_OPEN_PCT','0.75')
    monkeypatch.setattr(scanner,'spread_scan_exclude_delisted_exchange_cards',lambda:False)
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda:{})
    monkeypatch.setattr(funding,'_cache',{});monkeypatch.setattr(funding,'_inflight',set());monkeypatch.setattr(funding,'_queued',set())
    jobs=[];sell=[101.];book_calls=[]
    monkeypatch.setattr(funding,'_prefetch_pool',SimpleNamespace(submit=lambda f:jobs.append(f)))
    def book(client,exchange,market,symbol,aliases):
        book_calls.append(exchange);stamp=int(time.time()*1000);buy=market=='spot'
        return {'asks':[[100 if buy else sell[0]+.1,100]],'bids':[[99.9 if buy else sell[0],100]],
                'timestamp':stamp,'receivedAt':stamp,'localReceivedAt':stamp,'timestampSource':'exchange_depth_update',
                'exchange':exchange,'market':market,'requestDurationMs':1,'endpoint':'https://example.com',
                'bestAsk':100 if buy else sell[0]+.1,'bestBid':99.9 if buy else sell[0],
                'bestAskQuantity':100,'bestBidQuantity':100}
    monkeypatch.setattr(scanner,'_fetch_direct_depth_book',book)
    monkeypatch.setattr(scanner,'_fetch_sf_funding',lambda *a:{'code':'00000','data':[{'fundingRate':'0.001'}]})
    pair={'name':'ABC','type':'SF','buyEx':'binance','sellEx':'bitget','_buyVolume24hUsdt':1e6,'_sellVolume24hUsdt':1e6}
    result,report=scanner._fetch_direct_route_once_local(pair,sdk_config(),executable_depth_first=True)
    assert result is None and report['reason']=='sf_funding_pending'
    jobs.pop()();sell[0]=100.1
    result,report=scanner._fetch_direct_route_once_local(pair,sdk_config(),executable_depth_first=True)
    assert result is None and report['reason']=='below_threshold_or_rule_failed'
    assert len(book_calls)>=4
