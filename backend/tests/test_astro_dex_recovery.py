import pytest
from app import astro_spread_scanner as scanner
from app import astro_depth_transport as transport
from app.astro_route_policy import RoutePolicy, LatestQueue

@pytest.fixture
def state(monkeypatch):
    clock = [100.0]
    policy = RoutePolicy(clock=lambda: clock[0], wall=lambda: 1788690000 + clock[0])
    monkeypatch.setattr(scanner, '_route_policy', policy)
    monkeypatch.setattr(scanner, '_cloud_route_queue', LatestQueue())
    for key in ('_api_degraded_incidents', '_api_degraded_route_failures', '_depth_exchange_health',
                '_depth_backup_sources', '_api_recovery_probe_state'):
        monkeypatch.setattr(scanner, key, {})
    monkeypatch.setattr(transport, 'enabled', lambda: True)
    monkeypatch.setattr(transport, 'status', lambda: {'state': 'ready'})
    monkeypatch.setattr(scanner, '_fetch_queued_cloud_route', lambda *a, **k: pytest.fail('DEX must not call CEX cloud'))
    return policy, clock

@pytest.mark.parametrize('venue', ['okxdex', 'pancakeswapv3'])
@pytest.mark.parametrize('recovered_reason', ['eligible', 'below_threshold_or_rule_failed'])
def test_real_dex_observation_clears_fault_without_cloud_or_probe(state, monkeypatch, venue, recovered_reason):
    policy, clock = state
    pair = {'name':'4', 'type':'SF', 'buyEx':venue, 'sellEx':'binance'}
    observations = iter([
        (None, {'reason':'okxdex_executable_quote_unavailable', 'error':'SSL handshake timed out'}),
        (dict(pair) if recovered_reason == 'eligible' else None, {'reason':recovered_reason}),
    ])
    monkeypatch.setattr(scanner, '_fetch_direct_route_once_local', lambda *a, **k: next(observations))
    result, report = scanner._fetch_direct_route_once(pair, None)
    assert result is None and report['transport'] == 'local_proxy'
    assert report['reason'] == 'okxdex_executable_quote_unavailable'
    assert policy.snapshot()['affectedRouteCount'] == 1
    assert scanner._api_degraded_status()['active']
    assert scanner._active_api_recovery_sources() == []
    assert not scanner._depth_backup_sources and not scanner._api_degraded_incidents
    assert not policy.use_cloud(pair)
    clock[0] += 2
    scanner._fetch_direct_route_once(pair, None)
    status = scanner._api_degraded_status()
    assert not status['active'] and status['affectedSymbols'] == []
    row = status['routeControl']['routes'][0]
    assert row['category'] is None and row['lastSuccessfulVerificationAt']


def test_old_source_incident_does_not_override_current_route_evidence(state):
    policy, clock = state
    pair = {'name':'4', 'type':'SF', 'buyEx':'okxdex', 'sellEx':'binance'}
    scanner._api_degraded_incidents['okxdex'] = {'source':'okxdex','affectedSymbols':{'4'}}
    scanner._depth_exchange_health['okxdex'] = {'incidentOpen':True}
    assert scanner._active_api_recovery_sources() == []
    policy.observe(pair, {'reason':'direct_quote_unavailable'}, transport='local_proxy', available=False)
    assert scanner._api_degraded_status()['active']
    clock[0] += 7
    status = scanner._api_degraded_status()
    assert not status['active'] and status['affectedSymbols'] == []
    assert status['routeControl']['awaitingRecheckRouteCount'] == 1


def test_recovering_one_route_preserves_another_current_fault(state):
    policy, clock = state
    first = {'name':'4','type':'SF','buyEx':'okxdex','sellEx':'binance'}
    second = {**first,'name':'FONE'}
    for p in (first,second):
        policy.observe(p, {'reason':'direct_quote_unavailable'}, transport='local_proxy',available=False)
    policy.observe(first, {'reason':'eligible'}, transport='local_proxy',available=True)
    status=scanner._api_degraded_status()
    assert status['active'] and status['affectedSymbols'] == ['FONE']
    assert status['routeControl']['affectedRouteCount'] == 1


def test_cloud_observation_is_visible_without_claiming_failed_route(state):
    policy, clock=state
    pair={'name':'CEX','type':'FF','buyEx':'gate','sellEx':'binance'}
    policy.observe(pair, {'reason':'below_threshold_or_rule_failed'}, transport='tencent_cloud',available=True)
    status=scanner._api_degraded_status()
    assert status['active'] and status['affectedSymbols'] == ['CEX']
    assert status['routeControl']['affectedRouteCount'] == 0
    assert status['routeControl']['cloudRouteCount'] == 1
    clock[0] += 7
    assert not scanner._api_degraded_status()['active']


def test_unsupported_backup_activation_is_noop_and_cex_probes_remain(state):
    scanner._activate_cloud_backup({'name':'4','type':'SF','buyEx':'okxdex','sellEx':'binance'}, {'error':'timeout'})
    assert not scanner._depth_backup_sources and not scanner._api_degraded_incidents
    scanner._api_degraded_incidents.update({'gate':{},'pancakeswapv3':{},'okxdex':{}})
    assert scanner._active_api_recovery_sources() == ['gate']


def test_dex_quality_rejection_stays_visible_without_fault_notification(state,monkeypatch):
    policy,clock=state
    pair={'name':'4','type':'SF','buyEx':'okxdex','sellEx':'binance'}
    monkeypatch.setattr(scanner,'_fetch_direct_route_once_local',lambda *a,**k:(None,{'reason':'stale_direct_quote'}))
    for t in range(0,24,2):
        clock[0]=100+t
        scanner._fetch_direct_route_once(pair,None)
        assert policy.tick() is None
    status=scanner._api_degraded_status()
    assert status['active'] and status['routeControl']['affectedRouteCount']==1
    assert status['routeControl']['routes'][0]['category']=='quote_quality'
    assert not scanner._depth_backup_sources
