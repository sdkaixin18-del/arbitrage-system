import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from app import astro_depth_cloud as r
from app.astro_route_policy import category


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(r, '_dex_metrics', __import__('collections').defaultdict(int))
    monkeypatch.setattr(r, '_dex_inflight', {})
    monkeypatch.setattr(r, '_dex_cooldowns', {})
    monkeypatch.setattr(r, '_dex_last_started', {})
    monkeypatch.setattr(r, '_dex_slots', threading.BoundedSemaphore(3))
    monkeypatch.setattr(r, '_read_okx_credentials', lambda: {'K':'test','S':'test','P':'test'})


def test_official_solana_payment_and_amount(monkeypatch):
    def get(url, **kw):
        q=parse_qs(urlsplit(url).query)
        assert q['fromTokenAddress']==['Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB']
        assert q['amount']==['10000000']
        return type('Response',(),{'status_code':200,'json':lambda self:{'code':'0','data':[{'fromTokenAmount':'10000000','toTokenAmount':'1000000','fromToken':{'decimal':'6'},'toToken':{'decimal':'6'}}]}})()
    monkeypatch.setattr(r._client,'get',get)
    assert r._okx_quote('ABC','501','target',10)['fromAmount']=='10'


def test_coalesces_only_inflight_not_completed_and_fresh_independent():
    entered=threading.Event();release=threading.Event();calls=[]
    def fetch():
        calls.append(1);entered.set();assert release.wait(2);return {'quotedAt':len(calls)}
    def call(fresh=False):return r._scheduled_dex_quote('okxdex','501','token',10,fresh,fetch)
    with ThreadPoolExecutor(3) as pool:
        a=pool.submit(call);assert entered.wait(1)
        b=pool.submit(call)
        # Wait until the follower has joined, not until an arbitrary sleep expires.
        import time
        deadline=time.monotonic()+1
        while time.monotonic()<deadline and not r._dex_metrics.get('joinedInflight'):time.sleep(.001)
        release.set()
        assert a.result()['quotedAt']==b.result()['quotedAt']==1
        assert call(True)['quotedAt']==2
        assert call()['quotedAt']==3


def test_config_cooldown_chain_scope_other_chains_continue():
    def bad():raise r._dex_error('configuration','bad payment',status=422,retry=60)
    with pytest.raises(HTTPException):r._scheduled_dex_quote('okxdex','501','a',10,False,bad)
    with pytest.raises(HTTPException):r._scheduled_dex_quote('okxdex','501','b',10,False,lambda:pytest.fail('must not call upstream'))
    assert r._scheduled_dex_quote('okxdex','56','a',10,False,lambda:{'ok':True})['ok']


def test_no_route_only_cools_same_token():
    def bad():raise r._dex_error('no_route','no route',status=422,retry=30)
    with pytest.raises(HTTPException):r._scheduled_dex_quote('okxdex','501','a',10,False,bad)
    assert r._scheduled_dex_quote('okxdex','501','b',10,False,lambda:{'ok':True})['ok']


def test_queue_is_bounded(monkeypatch):
    monkeypatch.setattr(r,'_dex_slots',threading.BoundedSemaphore(0))
    with pytest.raises(HTTPException) as e:r._scheduled_dex_quote('okxdex','501','a',10,False,lambda:None)
    assert e.value.status_code==429


@pytest.mark.parametrize('kind,expected',[('configuration','configuration'),('no_route','no_route'),('rate_limit','capacity'),('timeout','transport')])
def test_error_categories(kind,expected):
    assert category({'reason':'okxdex_executable_quote_unavailable','error':f'DEX[{kind}] failure'})==expected

@pytest.mark.parametrize('status,code,msg,expected',[
    (429,'50011','Too many requests','rate_limit'),
    (200,'51000',"Token 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB' not supported",'configuration'),
    (200,'82000','Insufficient liquidity','no_route'),
])
def test_upstream_semantic_errors_not_generic_transport(monkeypatch,status,code,msg,expected):
    monkeypatch.setattr(r._client,'get',lambda *a,**kw:type('R',(),{'status_code':status,'json':lambda self:{'code':code,'msg':msg}})())
    with pytest.raises(HTTPException) as e:r._okx_quote('ABC','501','target',10)
    assert e.value.detail['category']==expected


def test_scanner_keeps_semantic_error_and_fresh_flag(monkeypatch):
    from app import astro_spread_scanner as s
    monkeypatch.setattr(s,'astro_min_notional_usdt',lambda:6)
    monkeypatch.setattr(s,'astro_max_notional_usdt',lambda c:10)
    class Client:
        def get(self,*args,**kw):
            assert kw['params']['fresh']=='true'
            return type('R',(),{'status_code':422,'json':lambda self:{'detail':{'category':'configuration','message':'bad payment','retryAfterSeconds':60}}})()
    with pytest.raises(RuntimeError,match=r'DEX\[configuration\]'):
        s._fetch_okxdex_executable_preflight(Client(),'ABC',{'chainIndex':'501','contractAddress':'target','_freshDexQuote':True},'gate',{},None)
