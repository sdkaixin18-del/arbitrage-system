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
