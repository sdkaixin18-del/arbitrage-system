import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.astro_route_policy import RoutePolicy, LatestQueue, QueueBusy, category

PAIR={"name":"Q","type":"SF","buyEx":"gate","sellEx":"aster"}


def policy():
    clock=[0.0]
    return RoutePolicy(clock=lambda:clock[0],wall=lambda:100000+clock[0]),clock


def fail(p,c,t,pair=PAIR,reason="cloud_depth_busy"):
    c[0]=t
    p.observe(pair,{"reason":reason},transport="tencent_cloud",available=False)


def good(p,c,t,pair=PAIR):
    c[0]=t
    p.observe(pair,{"reason":"below_threshold_or_rule_failed"},transport="local_proxy",available=True)


def test_failover_is_route_scoped_and_recovery_needs_all_failed_sources():
    p,c=policy();p.activate_cloud(PAIR,["gate","aster"])
    assert p.use_cloud(PAIR)
    assert not p.use_cloud({**PAIR,"name":"OTHER"})
    p.source_recovered("gate")
    assert p.use_cloud(PAIR)
    p.source_recovered("aster")
    assert not p.use_cloud(PAIR)


def test_local_quality_rejection_does_not_select_cloud_before_deadline():
    p,c=policy()
    p.observe(PAIR,{"reason":"stale_direct_quote"},transport="local_proxy",available=False,alertable=False)
    assert not p.use_cloud(PAIR)


@pytest.mark.parametrize("reason", ["stale_direct_quote", "direct_quote_time_skew", "direct_spread_unavailable"])
def test_cloud_quote_quality_stays_visible_but_never_sends_fault_push(reason):
    p, c = policy()
    p.activate_cloud(PAIR, ["gate"])
    for t in range(0, 1201, 2):
        fail(p, c, t, reason=reason)
        assert p.tick() is None
    row = p.snapshot()["routes"][0]
    assert row["mode"] == "paused"
    assert row["category"] == "quote_quality"
    assert p.snapshot()["affectedRouteCount"] == 1
    assert p.snapshot()["quoteQualityPushEnabled"] is False
    assert p.use_cloud(PAIR)  # No permission to manufacture a local recovery.


def test_quality_rejection_does_not_hide_later_real_transport_outage():
    p, c = policy()
    for t in range(0, 21, 2):
        fail(p, c, t, reason="stale_direct_quote")
    assert p.tick() is None
    for t in range(22, 43, 2):
        fail(p, c, t, reason="direct_quote_unavailable")
    action = p.tick()
    assert action["kind"] == "fault"
    assert p.prepare(action)[0]["category"] == "transport"


def test_pending_fault_is_cancelled_if_latest_result_is_only_quote_quality():
    p, c = policy()
    for t in range(0, 21, 2):
        fail(p, c, t, reason="direct_quote_unavailable")
    action = p.tick()
    fail(p, c, 21, reason="stale_direct_quote")
    assert p.prepare(action) is None


def test_old_paused_route_is_not_counted_as_a_current_outage_or_growing_duration():
    p, c = policy()
    fail(p, c, 0)
    fail(p, c, 4)
    assert p.snapshot()["affectedRouteCount"] == 1
    c[0] = 40
    snapshot = p.snapshot()
    assert snapshot["affectedRouteCount"] == 0
    assert snapshot["awaitingRecheckRouteCount"] == 1
    assert snapshot["routes"][0]["mode"] == "awaiting_recheck"
    assert snapshot["routes"][0]["durationSeconds"] == 4
    assert p.tick() is None


def test_valid_observation_clears_display_error_without_premature_recovery_push():
    p, c = policy()
    for t in range(0, 21, 2):
        fail(p, c, t)
    action = p.tick()
    p.prepare(action)
    p.delivered(action, True)
    good(p, c, 22)
    snapshot = p.snapshot()
    assert snapshot["affectedRouteCount"] == 0
    assert snapshot["routes"][0]["category"] is None
    assert snapshot["routes"][0]["error"] is None
    assert p.tick() is None


def test_fresh_routes_sort_before_old_unverified_failures():
    p, c = policy()
    fail(p, c, 0)
    good(p, c, 20, {**PAIR, "name": "CURRENT"})
    assert p.snapshot()["routes"][0]["symbol"] == "CURRENT"


def test_twenty_seconds_continuous_failure_and_global_throttle():
    p,c=policy()
    for t in range(0,20,2):
        fail(p,c,t);assert p.tick() is None
    fail(p,c,20)
    action=p.tick();assert action["kind"]=="fault"
    assert len(p.prepare(action))==1
    p.delivered(action,True)
    for t in range(22,50,2):
        fail(p,c,t);fail(p,c,t,{**PAIR,"name":"OTHER"})
        assert p.tick() is None


def test_single_failure_does_not_become_twenty_second_outage_by_waiting():
    p,c=policy();fail(p,c,0);c[0]=25
    assert p.tick() is None
    fail(p,c,25);assert p.tick() is None


def test_valid_quote_breaks_failure_continuity_and_local_only_fault_is_silent():
    p,c=policy()
    for t in range(0,20,2):fail(p,c,t)
    good(p,c,19);fail(p,c,20);assert p.tick() is None
    for t in range(22,50,2):
        c[0]=t;p.observe(PAIR,{"reason":"stale_direct_quote"},transport="local_proxy",available=False,alertable=False)
    assert p.tick() is None


def test_recovery_only_after_notified_and_ten_seconds_of_valid_observations():
    p,c=policy()
    for t in range(0,21,2):fail(p,c,t)
    action=p.tick();p.prepare(action);p.delivered(action,True)
    for t in range(22,32,2):good(p,c,t);assert p.tick() is None
    good(p,c,32);action=p.tick();assert action["kind"]=="recovery"
    p.prepare(action);p.delivered(action,True);assert p.tick() is None
    other,c2=policy()
    fail(other,c2,0)
    for t in range(2,20,2):good(other,c2,t)
    assert other.tick() is None


def test_queued_alert_is_rechecked_before_sending():
    p,c=policy()
    for t in range(0,21,2):fail(p,c,t)
    action=p.tick();good(p,c,21)
    assert p.prepare(action) is None


def test_throttle_survives_restart(tmp_path):
    p,c=policy();path=tmp_path/'state.json';p.configure_storage(path)
    for t in range(0,21,2):fail(p,c,t)
    action=p.tick();p.prepare(action);p.delivered(action,True)
    restored=RoutePolicy(clock=lambda:c[0],wall=lambda:100000+c[0]);restored.configure_storage(path)
    for t in range(22,50,2):fail(restored,c,t,{**PAIR,"name":"NEW"})
    assert restored.tick() is None


@pytest.mark.parametrize('report,expected',[
    ({'reason':'cloud_depth_busy'},'capacity'),
    ({'reason':'direct_quote_unavailable','error':'HTTP 429'},'capacity'),
    ({'reason':'stale_direct_quote'},'quote_quality'),
    ({'reason':'direct_quote_time_skew'},'quote_quality'),
    ({'reason':'direct_quote_unavailable','error':'SSL timeout'},'transport'),
    ({'reason':'open_spread_non_positive'},None),
    ({'reason':'cex_executable_depth_unavailable','error':'深度不足'},None),
])
def test_classification(report,expected):assert category(report)==expected


def test_queue_coalesces_pending_routes_and_prioritizes_new_work():
    q=LatestQueue(workers=1,capacity=3,ttl=1)
    entered,release=threading.Event(),threading.Event();calls=[]
    def block():entered.set();release.wait(2);return 'first'
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            first=pool.submit(q.run,'running',block)
            assert entered.wait(1)
            old=pool.submit(q.run,'same',lambda:calls.append('old') or 'old')
            end=time.monotonic()+1
            while q.snapshot()['waiting']<1 and time.monotonic()<end:time.sleep(.005)
            new=pool.submit(q.run,'same',lambda:calls.append('latest') or 'latest')
            urgent=pool.submit(q.run,'urgent',lambda:calls.append('urgent') or 'urgent',priority=0)
            end=time.monotonic()+1
            while (q.snapshot()['waiting']<2 or q.snapshot()['coalesced']<1) and time.monotonic()<end:time.sleep(.005)
            release.set()
            assert first.result()=='first'
            assert old.result()==new.result()=='latest'
            assert urgent.result()=='urgent'
        assert calls==['urgent','latest']
    finally:release.set();q.stop()


def test_queue_is_bounded_and_discards_old_waiters():
    q=LatestQueue(workers=1,capacity=1,ttl=.03)
    entered,release=threading.Event(),threading.Event()
    def block():entered.set();release.wait(2)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(q.run,'running',block);assert entered.wait(1)
            waiting=pool.submit(q.run,'waiting',lambda:pytest.fail('expired quote must not execute'))
            end=time.monotonic()+1
            while q.snapshot()['waiting']<1 and time.monotonic()<end:time.sleep(.005)
            with pytest.raises(QueueBusy):q.run('overflow',lambda:None)
            time.sleep(.04);release.set();first.result()
            with pytest.raises(QueueBusy):waiting.result()
    finally:release.set();q.stop()
