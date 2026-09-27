import json
from types import SimpleNamespace
import pytest
from app import astro_label_queue as q, astro_pulse_health as h

@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setattr(q,'_path',lambda:tmp_path/'queue.json')
    monkeypatch.setattr(h,'_sources',{})


def pair():
    return {'name':'ABC','type':'FF','buyEx':'gate','sellEx':'aster','_chainNote':'上架'}


def test_persistent_retry_backoff_and_server_ack():
    p=pair();card={**p,'id':'one'};q.enqueue(p,card)
    for now,delay,attempt in [(100,30,1),(130,120,2),(250,600,3)]:
        q.deliver_one([card],lambda *a:False,now=now)
        r=json.loads(q._path().read_text())['one']
        assert r['nextAt']==now+delay and r['attempts']==attempt
        q.deliver_one([card],lambda *a:pytest.fail('must wait'),now=now+1)
    assert q.status()['needsReview']==1
    q.deliver_one([card],lambda *a:True,now=850)
    assert q.status()['publishedToServer']==1
    assert q.status()['browserDisplayVerified'] is False
    q.enqueue(p,card)
    assert q.status()['pending']==0


@pytest.mark.parametrize('cards',[[],[{'id':'one','name':'OTHER'}],[{**pair(),'id':'replacement'}]])
def test_absent_or_changed_card_is_terminal_without_publish(cards):
    q.enqueue(pair(),{'id':'one'})
    q.deliver_one(cards,lambda *a:pytest.fail('no write'),now=100)
    assert json.loads(q._path().read_text())['one']['state']=='card_absent_or_changed'
    assert q.status()['pending']==0


def test_pancake_no_note_queue():
    q.enqueue({**pair(),'buyEx':'pancakeswapv3'},{'id':'one'})
    assert not q._path().exists()


def test_source_backoff_independent_and_two_success_recovery(monkeypatch):
    now=[100.];monkeypatch.setattr(h,'time',SimpleNamespace(monotonic=lambda:now[0]))
    for delay in [2,5,15,30]:
        h.result('bad',now[0],TimeoutError())
        assert not h.due('bad') and h.due('good')
        now[0]+=delay;assert h.due('bad')
    h.result('bad',now[0]);assert h.snapshot()['bad']['failures']==4
    h.result('bad',now[0]);assert h.snapshot()['bad']['failures']==0


def test_queue_status_failure_does_not_break_card_status(monkeypatch):
    q._path().write_text('invalid')
    assert q.status()['state']=='read_failed'


def test_lookup_failure_keeps_job_and_existing_note_without_thread(monkeypatch):
    from app import astro_sdk as sdk, astro_transfer_labels as labels
    monkeypatch.setattr(sdk, 'astro_chain_label_publish_enabled', lambda: True)
    monkeypatch.setattr(labels, 'collect', lambda *a: (_ for _ in ()).throw(RuntimeError('mapping unavailable')))
    monkeypatch.setattr(labels, 'routes', lambda *a: (_ for _ in ()).throw(RuntimeError('mapping unavailable')))
    monkeypatch.setattr(sdk.threading, 'Thread', lambda *a, **kw: pytest.fail('must use existing queue'))
    p = pair(); card = {**p, 'id': 'one'}
    sdk._queue_astro_chain_label_publish(p, card)
    now = q._load()['one']['nextAt']
    published = []
    q.deliver_one([card], lambda p, c: published.append(p['_chainNote']) or True, now=now)
    assert published == ['上架']
    assert q._load()['one']['state'] == 'pending_lookup'
    q.deliver_one([card], lambda *a: pytest.fail('must not repeat same note'), now=now+31)
    monkeypatch.setattr(labels, 'routes', lambda p: [('gate', 'ABC')])
    monkeypatch.setattr(labels, 'collect', lambda p: ('Gate提关', [{'status': 'ok'}]))
    q.deliver_one([card], lambda p, c: published.append(p['_chainNote']) or True, now=now+152)
    assert published == ['上架', '上架；Gate提关']
    assert q._load()['one']['state'] == 'published_to_server'


def test_partial_result_publishes_and_keeps_missing_leg_retry(monkeypatch):
    from app import astro_transfer_labels as labels
    monkeypatch.setattr(labels, 'routes', lambda p: [('gate', 'ABC'), ('bitget', 'ABC')])
    monkeypatch.setattr(labels, 'collect', lambda p: ('Gate提关', [{'status': 'ok'}, {'status': 'unknown'}]))
    p = pair(); card = {**p, 'id': 'one'}
    q.enqueue_lookup(p, card)
    now = q._load()['one']['nextAt']
    published = []
    q.deliver_one([card], lambda p, c: published.append(p['_chainNote']) or True, now=now)
    assert q._load()['one']['state'] == 'pending_lookup'
    monkeypatch.setattr(labels, 'collect', lambda p: ('Gate提关；Bitget提关', [{'status': 'ok'}, {'status': 'ok'}]))
    q.deliver_one([card], lambda p, c: published.append(p['_chainNote']) or True, now=now+31)
    assert published == ['上架；Gate提关', '上架；Gate提关；Bitget提关']
    assert q._load()['one']['state'] == 'published_to_server'


def test_worker_storage_error_is_logged_and_does_not_escape(monkeypatch):
    from app import astro_sdk as sdk
    events = []
    q._path().write_text('{')
    monkeypatch.setattr(sdk, '_log', lambda event, **kw: events.append(event))
    q.deliver_one([], lambda *a: pytest.fail('no publish'))
    assert events == ['astro_label_delivery_failed']
    assert not q._worker.locked()
    assert q._path().read_text() == '{'


def test_healthy_source_continues_during_other_source_backoff(monkeypatch):
    from app import astro_spread_scanner as scanner
    now=[100.];monkeypatch.setattr(h,'time',SimpleNamespace(monotonic=lambda:now[0]))
    monkeypatch.setattr(scanner,'PULSE_URLS',('good','bad'))
    called=[]
    def fetch(url):
        called.append(url)
        if url=='bad':raise TimeoutError('network')
        return {'code':0,'data':{'fresh':True}}
    monkeypatch.setattr(scanner,'_fetch_pulse',fetch)
    values,summary=scanner._fetch_direct_pulse_payloads()
    assert len(values)==1 and summary['failureCount']==1
    called.clear();values,summary=scanner._fetch_direct_pulse_payloads()
    assert called==['good'] and values==[{'code':0,'data':{'fresh':True}}]
    assert summary['failures'][0]['category']=='backoff'
