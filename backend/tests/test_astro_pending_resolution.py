from datetime import datetime, timedelta, timezone
import json
import httpx
import pytest
from app import astro_card_registry as r, astro_sdk as s
from test_astro_sdk import config, cleanup_fixture


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('ASTRO_AUTO_CARD_REGISTRY_FILE', str(tmp_path/'registry.json'))
    clock=[datetime(2026,9,8,tzinfo=timezone.utc)]
    monkeypatch.setattr(r,'_utc_now',lambda:clock[0])
    monkeypatch.setattr(s,'_log',lambda *a,**kw:None)
    monkeypatch.setattr(s,'_pending_submission_routes',set())
    monkeypatch.setattr(s,'_existing_route_snapshot',set())
    return clock


def pair(name='ABC'):
    return {'name':name,'type':'FF','buyEx':'aster','sellEx':'gate'}


@pytest.mark.parametrize('read_fails',[False,True])
def test_five_checks_stop_and_retain_lock(isolated,monkeypatch,read_fails):
    clock=isolated;start=clock[0];calls=[]
    class Client:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def list_pairs(self):
            calls.append(clock[0])
            if read_fails:raise httpx.ReadTimeout('')
            return []
    monkeypatch.setattr(s,'AstroSdkClient',Client)
    for name in ('ABC','XYZ'):r.record_pending_astro_submission(pair(name),'outcome_unknown')
    ids=[x['submissionId'] for x in r.pending_astro_submission_status()['items']]
    for seconds in range(0,80):
        clock[0]=start+timedelta(seconds=seconds)
        s._submission_confirmation_tick(config())
    assert [(x-start).total_seconds() for x in calls]==[2,5,15,30,60]
    state=r.pending_astro_submission_status()
    assert state['waitingCount']==0 and state['reviewCount']==2
    assert [x['submissionId'] for x in state['items']]==ids
    assert all(x['checkCount']==5 for x in state['items'])
    assert all(x['lastCheckOutcome']==('read_failed' if read_fails else 'not_uniquely_found') for x in state['items'])
    s._refresh_pending_submission_routes()
    assert s._pair_identity(pair()) in s._pending_submission_routes
    # Passive normal list reads can still resolve after the active deadline.
    s._replace_existing_route_snapshot([{**pair(),'id':'later-card'}])
    assert r.pending_astro_submission_status()['count']==1
    assert r.pending_astro_submission_status()['recentResolutions'][0]['state']=='confirmed'


def test_legacy_pending_expires_on_restart_without_erasing_record(isolated):
    r.record_pending_astro_submission(pair(),'outcome_unknown')
    payload=r._load_registry();key=next(iter(payload['pendingSubmissions']))
    payload['pendingSubmissions'][key].pop('submissionId');r._save_registry(payload)
    isolated[0]+=timedelta(minutes=40)
    assert r.pending_submission_checks_due()==[]
    first=r.pending_astro_submission_status()['items'][0]
    assert first['state']=='needs_review' and first['submissionId']
    assert r.pending_submission_checks_due()==[]
    assert r.pending_astro_submission_status()['items'][0]['submissionId']==first['submissionId']


def test_stale_poll_result_cannot_mark_new_submission(isolated):
    r.record_pending_astro_submission(pair(),'outcome_unknown');isolated[0]+=timedelta(seconds=2)
    due=r.pending_submission_checks_due()
    r.clear_pending_astro_submission(pair());r.record_pending_astro_submission(pair(),'submitting')
    r.record_submission_check(due,error='old read timeout')
    assert r.pending_astro_submission_status()['items'][0]['checkCount'] is None


@pytest.mark.parametrize('exc,unlocked',[
    (httpx.ConnectTimeout(''),True),(httpx.ConnectError(''),True),(httpx.PoolTimeout(''),True),
    (s.AstroSdkNotExecuted('rate limit'),True),
    (httpx.ReadTimeout(''),False),(httpx.WriteTimeout(''),False),
    (s.AstroSdkError('replay detected'),False),(s.AstroSdkError('unknown server failure'),False),
])
def test_only_proven_non_execution_releases_lock(isolated,monkeypatch,exc,unlocked):
    candidate,_=cleanup_fixture();calls=[]
    class Client:
        def list_pairs(self):return []
        def add_pair(self,p):calls.append(p);raise exc
    with pytest.raises(type(exc)):
        s._sync_candidate_pair(Client(),candidate,config(),None,set())
    status=s._refresh_pending_submission_routes()
    assert len(calls)==1
    assert (status['count']==0)==unlocked
    if unlocked:assert status['recentResolutions'][0]['state']=='failed_not_executed'
    else:assert type(exc).__name__ in status['items'][0]['error']


def test_request_metadata_keeps_exact_dex_fingerprint(isolated):
    p={'name':'ABC','type':'SF','buyEx':'okxdex','sellEx':'gate','_dexConfig':{'chainIndex':'56','contractAddress':'0xabc'}}
    r.record_pending_astro_submission(p,'submitting')
    r.record_submission_request(p,'request-nonce',123)
    r.record_pending_astro_submission(p,'outcome_unknown','timeout')
    state=r.pending_astro_submission_status()
    assert state['count']==1 and state['items'][0]['requestNonce']=='request-nonce'
    saved=next(iter(r._load_registry()['pendingSubmissions'].values()))['pair']
    assert r.pair_lifecycle_identity(saved)==r.pair_lifecycle_identity(p)


def test_manual_resolution_requires_review_state_and_keeps_evidence(isolated):
    r.record_pending_astro_submission(pair(),'outcome_unknown')
    submission=r.pending_astro_submission_status()['items'][0]['submissionId']
    with pytest.raises(ValueError):r.resolve_reviewed_submission(submission,'service log proves no request executed')
    isolated[0]+=timedelta(seconds=66);r.pending_submission_checks_due()
    with pytest.raises(ValueError):r.resolve_reviewed_submission(submission,'missing')
    evidence='Server log: core has no client connected; request was not executed.'
    result=r.resolve_reviewed_submission(submission,evidence)
    assert result['state']=='failed_not_executed'
    state=r.pending_astro_submission_status()
    assert state['count']==0 and evidence in state['recentResolutions'][0]['resolution']
    with pytest.raises(ValueError):r.resolve_reviewed_submission(submission,evidence)
