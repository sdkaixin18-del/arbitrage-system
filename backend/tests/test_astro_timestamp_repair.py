import time
from types import SimpleNamespace

import httpx
import pytest
from app import astro_spread_scanner as s
from app import crypto


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('STOCK_REVIEW_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(s, '_timestamp_repair_last', {})
    monkeypatch.setattr(s, '_timestamp_repair_counts', {})
    monkeypatch.setattr(s, 'spread_final_revalidation_max_skew_seconds', lambda: 1.25)
    monkeypatch.setattr(s, 'spread_final_revalidation_max_quote_age_seconds', lambda: 3)
    monkeypatch.setattr(s, 'spread_scan_sf_min_open_pct', lambda: .8)
    monkeypatch.setattr(s, 'spread_scan_ff_min_open_pct', lambda: .8)
    monkeypatch.setattr(s, 'spread_scan_max_open_pct', lambda: 10)
    monkeypatch.setattr(s, 'spread_cex_quote_notional_usdt', lambda *a: 20)


def book(stamp, bid=102, ask=100):
    return dict(timestamp=stamp, bids=[[bid, 10]], asks=[[ask, 10]],
                bestBid=bid, bestAsk=ask, receivedAt=stamp)


def repair(client, buy, sell, **kw):
    return s._repair_depth_timing(client, buy, sell, symbol='ABC', pair_type='SF',
        buy_exchange='gate', sell_exchange='bitget', aliases={}, deadline=kw.get('deadline', time.monotonic()+2))


@pytest.mark.parametrize('older_buy', [True, False])
def test_refreshes_only_older_leg_and_keeps_peer(monkeypatch, older_buy):
    now=int(time.time()*1000);old=book(now-2000);peer=book(now);fresh=book(now-100)
    calls=[]
    def fetch(client, exchange, market, *a):
        assert client.depth_refresh_started_ms >= now
        assert crypto.api_request_remaining_seconds() <= 1.5
        calls.append((exchange,market));return fresh
    monkeypatch.setattr(s,'_fetch_direct_depth_book',fetch)
    client=SimpleNamespace(depth_refresh_started_ms=0)
    b,z,r=repair(client,old if older_buy else peer,peer if older_buy else old)
    assert (b is fresh and z is peer) if older_buy else (b is peer and z is fresh)
    assert calls==[('gate','spot') if older_buy else ('bitget','future')]
    assert r['outcome']=='synchronized' and client.depth_refresh_started_ms==0


@pytest.mark.parametrize('case', ['aligned','peer_expired','budget'])
def test_unnecessary_or_impossible_refresh_makes_no_request(monkeypatch,case):
    now=int(time.time()*1000)
    monkeypatch.setattr(s,'_fetch_direct_depth_book',lambda *a:pytest.fail('unexpected request'))
    b,z=book(now-2000),book(now)
    if case=='aligned':b=book(now-10)
    if case=='peer_expired':b,z=book(now-6000),book(now-4000)
    _,_,r=repair(SimpleNamespace(),b,z,deadline=time.monotonic()+(0.01 if case=='budget' else 2))
    assert not r['attempted']


def test_cooldown_prevents_repeated_requests(monkeypatch):
    now=int(time.time()*1000);calls=[]
    monkeypatch.setattr(s,'_fetch_direct_depth_book',lambda *a:calls.append(1) or book(now-1800))
    b,z=book(now-2200),book(now)
    assert repair(SimpleNamespace(),b,z)[2]['outcome']=='still_unsynchronized'
    assert repair(SimpleNamespace(),b,z)[2]['outcome']=='cooldown'
    assert len(calls)==1


def test_timeout_retains_original_evidence(monkeypatch):
    now=int(time.time()*1000);b,z=book(now-2000),book(now)
    def fail(*a):raise TimeoutError('deadline')
    monkeypatch.setattr(s,'_fetch_direct_depth_book',fail)
    rb,rz,r=repair(SimpleNamespace(),b,z)
    assert rb is b and rz is z and r['outcome']=='refresh_failed'


def test_expiring_peer_cannot_be_called_synchronized(monkeypatch):
    real=time.time();monkeypatch.setattr(s.time,'time',lambda:real)
    def fetch(*a):
        monkeypatch.setattr(s.time,'time',lambda:real+2)
        return book(int((real+2)*1000))
    monkeypatch.setattr(s,'_fetch_direct_depth_book',fetch)
    _,_,r=repair(SimpleNamespace(),book(int((real-4)*1000)),book(int((real-2)*1000)))
    assert r['outcome']=='still_unsynchronized'


def test_reprices_both_legs_after_buy_refresh(monkeypatch):
    now=int(time.time()*1000);calls=[]
    def fetch(client,exchange,*a):
        calls.append(exchange)
        return book(now-2000 if calls.count('gate')==1 else now,ask=100 if calls.count('gate')==1 else 101) if exchange=='gate' else book(now,bid=102)
    monkeypatch.setattr(s,'_fetch_direct_depth_book',fetch)
    r=s._fetch_cex_executable_preflight(SimpleNamespace(),'ABC','SF','gate','bitget',{},None)
    assert calls.count('gate')==2 and calls.count('bitget')==1
    assert r['tokenQuantity']==pytest.approx(20/101)
    assert r['sellExecution']['targetQuantity']==pytest.approx(20/101)
    assert r['buyAveragePrice']==101 and r['timestampRepair']['outcome']=='synchronized'


def test_price_below_threshold_never_triggers_extra_request(monkeypatch):
    now=int(time.time()*1000);calls=[]
    monkeypatch.setattr(s,'_fetch_direct_depth_book',lambda c,ex,*a:calls.append(ex) or book(now-2000 if ex=='gate' else now,bid=100.1))
    r=s._fetch_cex_executable_preflight(SimpleNamespace(),'ABC','SF','gate','bitget',{},None)
    assert len(calls)==2 and not r['timestampRepair']['attempted']


def test_refresh_bypasses_completed_cache_from_older_request(monkeypatch):
    monkeypatch.setattr(s,'_depth_request_cache',{})
    monkeypatch.setattr(s,'_depth_request_inflight',set())
    monkeypatch.setattr(s,'_depth_request_errors',{})
    monkeypatch.setattr(s,'_before_depth_request',lambda *a:False)
    monkeypatch.setattr(s,'_record_depth_request_success',lambda *a,**kw:None)
    calls=[]
    def get(c,url,**kw):
        calls.append(1);return httpx.Response(200,json={'n':len(calls)},request=httpx.Request('GET',url))
    monkeypatch.setattr(s,'_scanner_public_get',get)
    c=SimpleNamespace();url='https://api.gateio.ws/api/v4/spot/order_book';params={'currency_pair':'ABC_USDT','limit':20}
    first,meta=s._shared_depth_json_get(c,url,params=params)
    assert s._shared_depth_json_get(c,url,params=params)[0]==first
    c.depth_refresh_started_ms=meta['requestStartedAt']+1
    assert s._shared_depth_json_get(c,url,params=params)[0]=={'n':2}
    assert len(calls)==2


def test_cloud_refresh_http_obeys_same_deadline():
    from app.astro_depth_transport import CloudPublicClient
    observed=[]
    url='https://api.gateio.ws/api/v4/spot/order_book'
    def response(request):
        observed.append(request.extensions['timeout'])
        now=int(time.time()*1000)
        return httpx.Response(200,json={'service':'astro-depth-cloud','url':url,'protocol':1,
            'receivedAtMs':now,'requestStartedAtMs':now-10,'statusCode':200,'payload':{'current':now}})
    c=CloudPublicClient.__new__(CloudPublicClient)
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(response),timeout=4.5) as c.client:
        with crypto.api_request_deadline(timeout_seconds=1):
            assert c.get(url,params={}).json()['current']>0
    assert all(0<v<=.25 for v in observed[0].values())


def test_refresh_price_fall_is_recomputed_not_original_signal(monkeypatch):
    now=int(time.time()*1000);calls=[]
    def fetch(c,ex,*a):
        calls.append(ex)
        return book(now-2000 if calls.count('gate')==1 else now,ask=100 if calls.count('gate')==1 else 104) if ex=='gate' else book(now,bid=102)
    monkeypatch.setattr(s,'_fetch_direct_depth_book',fetch)
    r=s._fetch_cex_executable_preflight(SimpleNamespace(),'ABC','SF','gate','bitget',{},None)
    assert r['executableSpreadPct']<0
    assert r['timestampRepair']['outcome']=='synchronized'


def test_expired_observation_does_not_start_supplement(monkeypatch):
    now=int(time.time()*1000);calls=[]
    monkeypatch.setattr(s,'_fetch_direct_depth_book',lambda c,ex,*a:calls.append(ex) or book(now-2000 if ex=='gate' else now))
    r=s._fetch_cex_executable_preflight(SimpleNamespace(depth_observation_deadline=time.monotonic()-.1),'ABC','SF','gate','bitget',{},None)
    assert len(calls)==2 and r['timestampRepair']['outcome']=='budget_exhausted'


def test_regressing_exchange_snapshot_cannot_replace_original(monkeypatch):
    now=int(time.time()*1000);b,z=book(now-2000),book(now)
    monkeypatch.setattr(s,'_fetch_direct_depth_book',lambda *a:book(now-2500))
    rb,rz,r=repair(SimpleNamespace(),b,z)
    assert rb is b and rz is z and r['outcome']=='snapshot_regressed'
