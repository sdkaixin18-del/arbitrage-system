import importlib.util
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
from app import astro_sdk as sdk, exchange_announcements as news
from test_astro_sdk import config, cleanup_fixture

@pytest.mark.parametrize('original,current', [('',None),(None,''),(None,None)])
def test_explicit_disabled_alert_null_is_compatible(original,current):
    pair,record=cleanup_fixture();pair.pop('priceAlertOnlyRise');pair['priceAlert']=current
    record['createdPair']['priceAlert']=original;record['submittedOnlyConfigFields']=['priceAlertOnlyRise']
    assert sdk._cleanup_safety_check(record,pair)[0]
    for changes in [{'status':True},{'aExPosition':1},{'maxTradeUSDT':'999'},{'priceAlert':'0.02'}]:
        assert not sdk._cleanup_safety_check(record,{**pair,**changes})[0]
    pair.pop('priceAlert');assert not sdk._cleanup_safety_check(record,pair)[0]
    pair['priceAlert']=None;record['createdPair'].pop('priceAlert')
    assert not sdk._cleanup_safety_check(record,pair)[0]

def test_sdk_lease_reuses_real_connection_across_sync_threads(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_POST(self):
            assert json.loads(self.rfile.read(int(self.headers['Content-Length'])))=={'action':'list'}
            body=b'{"code":0,"data":[]}'
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    monkeypatch.setattr(sdk,'_sdk_idle_clients',deque())
    cfg=config(base_url=f'http://127.0.0.1:{server.server_port}');clients=[];samples=[]
    def read():
        with sdk._reusable_sdk_client(cfg) as client:
            assert client.list_pairs(deadline=time.monotonic()+2)==[]
            clients.append(client);samples.append(dict(client._io_timing))
    try:
        for _ in range(2):
            with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(read).result()
        assert clients[0] is clients[1]
        assert not samples[0]['connectionReused'] and samples[1]['connectionReused']
        with sdk._reusable_sdk_client(cfg) as client:client.connection_failed=True
        assert not sdk._sdk_idle_clients and client.client.is_closed
    finally:
        while sdk._sdk_idle_clients:sdk._sdk_idle_clients.pop()[1].close()
        server.shutdown();server.server_close()

def test_sdk_lease_separates_accounts_and_excludes_concurrent_borrow(monkeypatch):
    class Client:
        def __init__(self,cfg):self.config=cfg;self.connection_failed=False;self.closed=False
        def close(self):self.closed=True
    monkeypatch.setattr(sdk,'AstroSdkClient',Client);monkeypatch.setattr(sdk,'_sdk_client_type',Client)
    monkeypatch.setattr(sdk,'_sdk_idle_clients',deque())
    with sdk._reusable_sdk_client(config()) as one:
        with sdk._reusable_sdk_client(config()) as two:assert one is not two
    with sdk._reusable_sdk_client(config(api_key='other-test-key')) as other:
        assert other is not one and other is not two
    while sdk._sdk_idle_clients:sdk._sdk_idle_clients.pop()[1].close()

def test_announcement_timeout_keeps_one_inflight_and_recovers_result(monkeypatch):
    release=threading.Event();calls=[];pool=ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(news,'_announcement_executor',pool);monkeypatch.setattr(news,'_announcement_futures',{})
    monkeypatch.setattr(news,'_announcement_completed',{});monkeypatch.setattr(news,'_FETCH_BUDGET_SECONDS',.03)
    def slow():calls.append(1);assert release.wait(2);return [{'title':'slow-result'}]
    try:
        first=news.fetch_sources([('gate',slow),('bn',lambda:[{'title':'fast'}])])
        assert first[0][2] and first[1][1]==[{'title':'fast'}]
        assert news.fetch_sources([('gate',slow)])[0][2] and len(calls)==1
        release.set();news._announcement_futures['gate'].result(timeout=1)
        assert news.fetch_sources([('gate',slow)])[0]==('gate',[{'title':'slow-result'}],None)
        assert len(calls)==1 and not news._announcement_futures
    finally:release.set();pool.shutdown()

def test_source_retry_is_bounded_and_freshness_not_reset(monkeypatch):
    assert news.source_retry_seconds('gate',{'consecutive_failures':1})==60
    assert news.source_retry_seconds('gate',{'consecutive_failures':4})==300
    monkeypatch.setattr(news,'_source_cache',{'gate':{'items':[{'title':'old'}],'last_success_at':'old',
        'last_success_monotonic':time.monotonic()-1000,'last_attempt_monotonic':time.monotonic(),
        'last_error':'timeout','consecutive_failures':4}})
    monkeypatch.setattr(news,'fetch_sources',lambda *a:pytest.fail('must not retry during backoff'))
    _,rows,state=news.fetch_announcement_sources([('gate',lambda:[])],force_refresh=False)[0]
    assert rows and state['stale'] and state['status']=='partial_error' and state['last_success_at']=='old'

def test_gate_sections_are_concurrent_and_complete(monkeypatch):
    barrier=threading.Barrier(3)
    monkeypatch.setattr(news,'GATE_SECTION_URLS',[(str(n),'spot',str(n)) for n in range(3)])
    monkeypatch.setattr(news,'gate_section_url_candidates',lambda url:[url])
    class Response:
        def __init__(self,url):self.text=url
        def raise_for_status(self):pass
    def get(client,url):barrier.wait(timeout=2);return Response(url)
    monkeypatch.setattr(news,'get_response',get)
    monkeypatch.setattr(news,'extract_gate_next_data_articles',lambda text,**kw:[{'title':text}])
    assert news.fetch_gate_via_next_data(object())==[{'title':str(n)} for n in range(3)]

def load_bridge():
    path=Path(__file__).resolve().parents[2]/'launcher/pulse_ssh_bridge.py'
    spec=importlib.util.spec_from_file_location('pulse_reliability_bridge',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

def test_pulse_queue_timeout_does_not_close_another_request(tmp_path):
    mod=load_bridge();key=tmp_path/'key';key.write_text('test');key.chmod(0o600)
    bridge=mod.PersistentPulseSSH('ubuntu@example.com',key)
    bridge.lock.acquire();started=time.monotonic()
    try:
        with pytest.raises(TimeoutError,match='queue deadline'):bridge.fetch(.01)
        assert time.monotonic()-started<1.2 and bridge.lock.locked()
    finally:bridge.lock.release()

@pytest.mark.parametrize('during_async_setup',[False,True])
def test_before_send_deadline_is_definitely_not_executed(monkeypatch,during_async_setup):
    import httpx
    import asyncio
    from types import SimpleNamespace
    calls=[];now=[100.0]
    monkeypatch.setattr(sdk,'time',SimpleNamespace(monotonic=lambda:now[0],time=time.time))
    def handler(request):calls.append(request);return httpx.Response(200,json={'code':0,'data':[]})
    with sdk.AstroSdkClient(config(),transport=httpx.MockTransport(handler)) as client:
        real_client=httpx.AsyncClient
        if during_async_setup:
            def factory(**kwargs):
                now[0]=102.0
                return real_client(**kwargs)
            monkeypatch.setattr(httpx,'AsyncClient',factory)
        loop=asyncio.new_event_loop();client._deadline_loop=loop
        with pytest.raises(sdk.AstroSdkBeforeSendExpired) as error:
            loop.run_until_complete(client._post_until('http://example.com',content=b'{}',headers={},deadline=101 if during_async_setup else 99))
        assert isinstance(error.value,sdk.AstroSdkNotExecuted)
        assert calls==[]


def test_timeout_after_http_start_stays_uncertain(monkeypatch):
    import httpx
    import asyncio
    async def handler(request):
        await asyncio.sleep(.1)
        return httpx.Response(200,json={'code':0,'data':[]})
    with sdk.AstroSdkClient(config(),transport=httpx.MockTransport(handler)) as client:
        loop=asyncio.new_event_loop();client._deadline_loop=loop
        with pytest.raises(TimeoutError) as error:
            loop.run_until_complete(client._post_until('http://example.com',content=b'{}',headers={},deadline=time.monotonic()+.02))
        assert not isinstance(error.value,sdk.AstroSdkNotExecuted)
