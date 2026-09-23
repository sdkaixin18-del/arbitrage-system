import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path
import pytest
from app import astro_transfer_labels as mod, astro_sdk as sdk


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(mod,'_CACHE',{})
    monkeypatch.setattr(mod,'_PENDING',{})
    from app import astro_spread_scanner as scanner
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda:{})


def test_live_nes_response_shapes_and_empty_bybit():
    fixture=Path(__file__).resolve().parent/'fixtures/astro_transfer_nes.json'
    data=json.loads(fixture.read_text())
    for entry in data[:4]:
        ex=entry['exchange'];coin='NESA' if ex=='bybit' else 'NES'
        rows=mod.normalize(ex,coin,entry['payload'])
        assert mod.label(ex,rows)==('' if ex=='bybit' else mod._NAMES[ex]+'单机币')


def test_partial_network_closure_is_not_exchange_wide():
    rows=[{'chain':'ETH','deposit':False,'withdraw':False},{'chain':'BSC','deposit':True,'withdraw':True}]
    assert mod.label('gate',rows)=='Gate(ETH充提关)'
    rows[1]['deposit']=None
    assert '单机币' not in mod.label('gate',rows)
    assert mod.label('gate',[{'chain':'ETH','deposit':False,'withdraw':True}])=='Gate充关'
    assert mod.label('gate',[{'chain':'ETH','deposit':True,'withdraw':False}])=='Gate提关'
    assert mod.label('gate',[{'chain':'ETH','deposit':None,'withdraw':None}])==''


def test_missing_fields_and_wrong_asset_never_become_closed():
    assert mod.normalize('gate','NES',{'currency':'OTHER','chains':[{'name':'ETH','deposit_disabled':True}]})==[]
    rows=mod.normalize('bitget','NES',{'code':'00000','data':[{'coin':'NES','chains':[{'chain':'ETH'}]}]})
    assert mod.label('bitget',rows)==''


def test_nonblocking_prefetch_deduplicates_and_caps_pending(monkeypatch):
    submitted=[]
    class Pool:
        def submit(self,fn,key):
            submitted.append(key);return Future()
    monkeypatch.setattr(mod,'_POOL',Pool())
    pair={'type':'FF','name':'NES','buyEx':'gate','sellEx':'bitget'}
    mod.prefetch(pair);mod.prefetch(pair)
    assert len(submitted)==2
    for i in range(30):mod.prefetch({**pair,'name':f'X{i}'})
    assert len(submitted)==mod.MAX_PENDING
    start=time.monotonic();note,_=mod.collect(pair,timeout=.01)
    assert time.monotonic()-start<.2 and note==''


def test_cache_expiry_and_exact_spot_alias(monkeypatch):
    from app import astro_spread_scanner as scanner
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda:{('bybit','spot','NESA'):('NES',1)})
    pair={'type':'FF','name':'NES','buyEx':'bybit','sellEx':'gate'}
    assert mod.routes(pair)==[('bybit','NESA'),('gate','NES')]
    monkeypatch.setattr(mod,'prefetch',lambda p:None)
    mod._CACHE[('gate','NES')]=(time.monotonic(),{'status':'ok','chains':[{'chain':'ETH','deposit':False,'withdraw':False}]})
    assert mod.collect(pair)[0]=='Gate单机币'
    mod._CACHE[('gate','NES')]=(time.monotonic()-61,mod._CACHE[('gate','NES')][1])
    assert mod.collect(pair)[0]==''
    assert mod.routes({**pair,'buyEx':'pancakeswapv3'})==[]


def test_publication_combines_existing_note_and_transfer_remark(monkeypatch):
    captured=[]
    monkeypatch.setattr(sdk,'astro_chain_label_publish_enabled',lambda:True)
    monkeypatch.setattr(mod,'collect',lambda p:('Gate单机币',[]))
    monkeypatch.setattr(sdk,'_log',lambda *a,**k:None)
    monkeypatch.setattr(sdk.astro_label_queue,'enqueue',lambda p,c:captured.append(p['_chainNote']))
    class Immediate:
        def __init__(self,target,**kw):self.target=target
        def start(self):self.target()
    monkeypatch.setattr(sdk.threading,'Thread',Immediate)
    sdk._queue_astro_chain_label_publish({'name':'NES','_chainNote':'上架'},{'id':'test'})
    assert captured==['上架；Gate单机币']
    sdk._queue_astro_chain_label_publish({'name':'NES','buyEx':'pancakeswapv3'},{'id':'test2'})
    assert len(captured)==1
