"""Keep market identity, spread direction, gaps and read-only caching intact."""
import math
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app import book_spread_history as b
from app import dex_history as h


def bar(ts=3540, lb=99, la=100, sb=102, sa=103):
    # Deliberately wrong vendor percentages: only raw BBO may determine our lines.
    return {"time":ts,"l_bid":{"close":lb},"l_ask":{"close":la},
            "s_bid":{"close":sb},"s_ask":{"close":sa},"in":{"close":999},"out":{"close":999}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    h._cache.clear(); h._inflight.clear(); b._series.clear()
    monkeypatch.setattr(b.time,"time",lambda:3600)
    yield
    h._cache.clear(); h._inflight.clear(); b._series.clear()


def test_raw_bbo_and_local_exit_direction_ignore_vendor_percentages():
    points,_=b.normalize_bars([bar()],0,3600,60,1,1)
    assert points[0]['openSpreadPct']==pytest.approx(200*(102-100)/(102+100))
    assert points[0]['closeSpreadPct']==pytest.approx(200*(103-99)/(103+99))
    assert points[0]['closeSpreadPct']>0


def test_per_coin_divisors_apply_to_both_bid_and_ask():
    points,_=b.normalize_bars([bar(lb=99000,la=100000)],0,3600,60,1000,1)
    assert points[0]['leftBid']==99 and points[0]['leftAsk']==100
    assert points[0]['openSpreadPct']==pytest.approx(200*2/202)


def test_invalid_duplicate_conflict_and_unfinished_bars_leave_gaps():
    rows=[bar(3420),bar(3420),bar(3480),bar(3480,sb=101),bar(3540,lb=101,la=100),bar(3600),bar(3300,la=float('nan')),bar(3360,sa=0)]
    points,quality=b.normalize_bars(rows,0,3600,60,1,1)
    assert [p['timestamp'] for p in points]==[3420000]
    assert quality=={'invalidBars':3,'duplicateBars':2,'conflictingBars':1}


def markets():
    return [dict(exchange='asterdex',symbol='BTCUSDT',asset='BTC',spot=False),dict(exchange='gateio',symbol='BTC_USDT',asset='BTC',spot=False)]


def test_sf_never_substitutes_perpetual_market():
    assert b._resolve_market(markets(),'asterdex','BTC',True) is None
    available=markets()+[dict(exchange='asterdex_spot',symbol='BTCUSDT',asset='BTC',spot=True)]
    assert b._resolve_market(available,'asterdex','BTC',True)['exchange']=='asterdex_spot'
    assert b._resolve_market(available,'asterdex','BTC',False)['exchange']=='asterdex'


def test_ambiguous_market_is_not_silently_selected():
    assert b._resolve_market(markets()+[markets()[0]],'asterdex','BTC',False) is None


def query(**kwargs):
    values=dict(pair_type='FF',left_venue='aster',right_venue='gate',left_symbol='BTC',right_symbol='BTC',range_hours=24,left_divisor=1,right_divisor=1)
    return b.book_history(**{**values,**kwargs})


def test_unsupported_dex_sf_is_explicit_and_does_not_call_upstream(monkeypatch):
    monkeypatch.setattr(b,'_markets',lambda:pytest.fail('DEX must not query perpetual catalog'))
    result=query(pair_type='SF',left_venue='okxdex')
    assert result['status']=='unsupported' and result['points']==[]


def test_sf_unsupported_when_only_futures_are_listed(monkeypatch):
    monkeypatch.setattr(b,'_markets',markets)
    monkeypatch.setattr(b,'_load_points',lambda *a:pytest.fail('wrong market'))
    assert query(pair_type='SF')['status']=='unsupported'


def test_sf_uses_real_spot_when_available(monkeypatch):
    monkeypatch.setattr(b,'_markets',lambda:markets()+[dict(exchange='asterdex_spot',symbol='BTCUSDT',asset='BTC',spot=True)])
    captured=[]
    def load(left,right,*a):
        captured.append((left['exchange'],right['exchange']));return [],{}
    monkeypatch.setattr(b,'_load_points',load)
    assert query(pair_type='SF')['status']=='empty'
    assert captured==[('asterdex_spot','gateio')]


def test_identity_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(b,'_markets',lambda:[markets()[0],{**markets()[1],'asset':'OTHER_BTC'}])
    with pytest.raises(HTTPException) as exc:query()
    assert exc.value.status_code==422


def test_history_refresh_is_incremental_and_overlapping_errors_remove_old_points(monkeypatch):
    calls=[]
    def upstream(path,params):
        calls.append(params)
        if len(calls)==1:return {'source':'archive_15s','bars':[bar(3420),bar(3480),bar(3540)]}
        return {'source':'archive_15s','bars':[bar(3480,lb=105),bar(3540),bar(3600)]}
    monkeypatch.setattr(b,'_get_json',upstream)
    left,right=markets()
    first,_=b._load_points(left,right,0,3600,60,1,1)
    second,quality=b._load_points(left,right,60,3660,60,1,1)
    assert len(first)==3
    assert quality['incremental'] is True
    assert calls[1]['minutes']==3
    assert [p['timestamp'] for p in second]==[3420000,3540000,3600000]
    assert calls[0]['to']==3540


def test_bucket_size_route_and_amount_normalization_have_separate_caches(monkeypatch):
    calls=[]
    def upstream(path,params):calls.append(params);return {'source':'archive_15s','bars':[]}
    monkeypatch.setattr(b,'_get_json',upstream)
    left,right=markets()
    b._load_points(left,right,0,3600,60,1,1)
    _,quality=b._load_points(left,right,0,3600,60,1000,1)
    assert not quality['incremental']
    _,quality=b._load_points(right,left,0,3600,60,1,1)
    assert not quality['incremental']


def test_initial_empty_history_can_be_backfilled_on_next_request(monkeypatch):
    calls=[]
    def upstream(path,params):calls.append(params);return {'source':'archive_15s','bars':[]}
    monkeypatch.setattr(b,'_get_json',upstream)
    left,right=markets()
    b._load_points(left,right,0,3600,60,1,1)
    _,quality=b._load_points(left,right,60,3660,60,1,1)
    assert not quality['incremental'] and calls[-1]['minutes']==60


def test_api_status_coverage_and_validations(monkeypatch):
    monkeypatch.setattr(b,'_markets',markets)
    monkeypatch.setattr(b,'_get_json',lambda *a,**kw:{'source':'archive_15s','bars':[bar()]})
    app=FastAPI();app.include_router(h.router)
    client=TestClient(app)
    params=dict(pairType='FF',leftVenue='aster',rightVenue='gate',leftSymbol='BTC',rightSymbol='BTC',rangeHours=24)
    response=client.get('/api/dex-history/books',params=params)
    assert response.status_code==200
    data=response.json()
    assert data['stats']['pointCount']==1 and data['stats']['expectedPointCount']==1440
    assert data['sourceQuoteTimesAvailable'] is False
    for patch in [{'leftDivisor':0},{'rightDivisor':'nan'},{'rightDivisor':'inf'},{'leftSymbol':'<script>'},{'pairType':'FS'},{'rangeHours':9},{'leftVenue':'unknown'}]:
        assert client.get('/api/dex-history/books',params={**params,**patch}).status_code in (400,422)


def test_http_200_business_error_is_not_success(monkeypatch):
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'error':'market absent','bars':[]}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,*args,**kwargs):return Response()
    monkeypatch.setattr(b.httpx,'Client',Client)
    with pytest.raises(HTTPException) as exc:b._get_json('/api/arbitrage/live-pair-history',{})
    assert exc.value.status_code==422


def test_route_catalog_includes_ff_and_cex_sf_without_changing_dex_cards():
    cards=[dict(id='ff',type='FF',name='BTC',buyEx='aster',sellEx='gate'),dict(id='sf',type='SF',name='BTC',buyEx='binance',sellEx='gate')]
    result=h.build_route_catalog(cards,[],[],[],{})
    assert result['cards']==[]
    assert [c['type'] for c in result['bookCards']]==['FF','SF']


def test_source_series_capacity_is_bounded(monkeypatch):
    monkeypatch.setattr(b,'_get_json',lambda *a,**kw:{'source':'archive_15s','bars':[]})
    left,right=markets()
    for i in range(20):b._load_points({**left,'symbol':f'COIN{i}USDT'},right,0,3600,60,1,1)
    assert len(b._series)==16
