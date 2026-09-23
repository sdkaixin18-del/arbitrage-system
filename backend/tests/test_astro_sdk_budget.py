import threading
from types import SimpleNamespace
import pytest
import httpx
from app import astro_sdk_budget as budget
from app import astro_sdk as sdk
from test_astro_sdk import config


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    fake = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(budget, 'time', fake)
    monkeypatch.setattr(sdk, 'time', SimpleNamespace(**vars(fake), time=lambda: 1800000000+now[0]))
    monkeypatch.setattr(budget, '_budgets', {})
    return now


def test_all_actions_share_sliding_window_without_sleeping_on_write(clock):
    b = budget.budget_for('https://example.com/a')
    assert b is budget.budget_for('https://example.com/b')
    for _ in range(16):b.wait(clock[0]+3, consume=True)
    with pytest.raises(budget.BudgetDeferred):b.wait(clock[0]+30, consume=True, no_wait=True)
    assert clock[0] == 100
    with pytest.raises(budget.BudgetDeferred):b.wait(clock[0]+3)
    b.wait(clock[0]+11, slots=3)
    assert clock[0] == pytest.approx(110.1)
    b.wait(clock[0]+3, consume=True)
    assert b.snapshot()['recentRequests'] == 1


def test_listing_pacing_before_quote_and_429_cooldown(clock):
    b = budget.RequestBudget()
    b.listing_submitted()
    b.wait(clock[0]+11, slots=3, listing=True)
    assert clock[0] == 103
    b.limited('12')
    with pytest.raises(budget.BudgetDeferred):b.wait(clock[0]+11)
    clock[0] += 12
    b.wait(clock[0]+3, consume=True)
    assert b.snapshot()['rateLimited'] == 1


def test_concurrent_reads_cannot_exceed_quota(clock):
    b = budget.RequestBudget();accepted=[]
    def request():
        try:b.wait(200, consume=True, no_wait=True);accepted.append(1)
        except budget.BudgetDeferred:pass
    ts=[threading.Thread(target=request) for _ in range(40)]
    for t in ts:t.start()
    for t in ts:t.join()
    assert len(accepted)==16


def test_limited_add_is_known_not_executed_and_other_client_waits(clock):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(429, text='rate limited', headers={'Retry-After':'10'})
    with sdk.AstroSdkClient(config(),transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(sdk.AstroSdkNotExecuted):c.add_pair({'name':'A','status':False})
    with sdk.AstroSdkClient(config(),transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(sdk.AstroSdkRateDeferred):c.list_pairs(deadline=clock[0]+3)
    assert len(calls)==1


def test_fast_failed_list_can_retry_within_total_budget(clock,monkeypatch):
    calls=[];events=[]
    monkeypatch.setattr(sdk,'_log',lambda *a,**kw:events.append((a,kw)))
    class Client:
        deadline_support=True
        def list_pairs(self,*,deadline):
            calls.append(deadline)
            if len(calls)==1:
                clock[0]+=0.5
                raise TimeoutError('first connection stalled')
            clock[0]+=.2
            return [{'id':'fresh'}]
    assert sdk._list_pairs_with_retry(Client(),deadline=103)==[{'id':'fresh'}]
    assert calls==[103,103]
    assert clock[0]<103
    assert events[0][0][0]=='astro_sdk_list_recovered'


def test_budget_delay_happens_before_revalidation(clock,monkeypatch):
    # Integration ordering without creating any remote card.
    events=[]
    class Client:
        def prepare_creation(self,**kwargs):events.append('budget');return 3000
    pair={'name':'ABC','type':'FF','buyEx':'binance','sellEx':'gate','openPosition':'0.01','disableOpen':False}
    monkeypatch.setattr(sdk,'_prepare_dex_route',lambda *a:(True,{}))
    monkeypatch.setattr(sdk,'_refresh_pending_submission_routes',lambda:None)
    monkeypatch.setattr(sdk,'_pending_submission_routes',set())
    monkeypatch.setattr(sdk,'astro_chain_label_publish_enabled',lambda:False)
    monkeypatch.setattr(sdk,'_log',lambda *a,**kw:None)
    def validate(*a):events.append('quote');return None,{'reason':'no_opportunity'}
    assert not sdk._sync_candidate_pair(Client(),pair,config(),validate,set(),None)
    assert events==['budget','quote']


def test_slow_successful_handshake_is_not_cancelled_at_1250ms(clock):
    class Client:
        deadline_support=True
        def list_pairs(self, *, deadline):
            assert deadline==103
            clock[0]+=2.4
            return [{'id':'fresh'}]
    assert sdk._list_pairs_with_retry(Client(),deadline=103)==[{'id':'fresh'}]
