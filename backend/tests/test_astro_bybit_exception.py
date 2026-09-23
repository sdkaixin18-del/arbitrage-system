import json
import time
import pytest
from app import astro_sdk as sdk, astro_spread_scanner as scanner
from test_astro_spread_scanner import market, sdk_config, cex_preflight


@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    path=tmp_path/'settings.json';path.write_text(json.dumps({'ffBybitSellExceptionEnabled':True}))
    monkeypatch.setenv('ASTRO_SPREAD_SUBSCRIPTIONS_FILE',str(path))
    monkeypatch.setenv('ASTRO_SPREAD_MAX_OPEN_PCT','10')
    monkeypatch.setenv('ASTRO_SPREAD_MIN_VOLUME_USDT','0')
    monkeypatch.setenv('ASTRO_SPREAD_STRUCTURE_FILTER_ENABLED','0')
    monkeypatch.setenv('ASTRO_CHAIN_LABEL_PUBLISH_ENABLED','0')
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda:{})
    return path


@pytest.mark.parametrize('gap,allowed',[(9.99,False),(10,False),(10.0001,True),(15,True),(float('nan'),False)])
def test_strict_threshold_across_route_build_and_candidate(gap,allowed):
    c={'type':'FF','symbol':'ABC','buyExchange':'gate','sellExchange':'bybit','openSpreadPct':gap}
    assert bool(sdk.astro_spread_card_routes(c)) is allowed
    assert scanner._meets_auto_card_rule(c) is allowed


def test_disable_and_other_routes_keep_previous_limits(isolated):
    c={'type':'FF','symbol':'ABC','buyExchange':'gate','sellExchange':'bybit','openSpreadPct':15}
    isolated.write_text(json.dumps({'ffBybitSellExceptionEnabled':False}))
    assert not sdk.astro_spread_card_routes(c)
    assert not scanner._meets_auto_card_rule(c)
    assert not scanner._meets_auto_card_rule({**c,'sellExchange':'binance'})
    assert sdk.astro_spread_card_routes({**c,'buyExchange':'bybit','sellExchange':'gate','openSpreadPct':1})


def test_pulse_discovery_bybit_sell_above_ten(monkeypatch):
    monkeypatch.setenv('ASTRO_SPREAD_MARKETS','gateFuture,bybitFuture')
    p={'data':{'gateFuture':market(1000000,[{'name':'ABCUSDT','a':100,'b':99.9,'trade24Count':1000000}]),
               'bybitFuture':market(1000000,[{'name':'ABCUSDT','a':113,'b':112,'trade24Count':1000000}])}}
    cs,_=scanner.scan_pulse_spreads([p],now_ms=1001000,symbol_aliases={})
    assert [(c['buyExchange'],c['sellExchange']) for c in cs]==[('gate','bybit')]
    assert scanner._meets_auto_card_rule(cs[0])


@pytest.mark.parametrize('sell,allowed',[(112,True),(108,False)])
def test_real_depth_overrides_pulse_gap(monkeypatch,sell,allowed):
    monkeypatch.setattr(scanner,"spread_scan_exclude_delisted_exchange_cards",lambda:False)
    now=int(time.time()*1000)
    monkeypatch.setattr(scanner,'_fetch_direct_quote',lambda _client,ex,m,*a:{'exchange':ex,'market':m,'rawSymbol':'ABC','bid':99.9 if ex=='gate' else 112,'ask':100 if ex=='gate' else 113,'timestamp':now,'timestampSource':'exchange'})
    monkeypatch.setattr(scanner,'_fetch_cex_executable_preflight',lambda *a,**k:cex_preflight(now,100,sell))
    pair={'name':'ABC','type':'FF','buyEx':'gate','sellEx':'bybit','openPosition':'0.15','_buyVolume24hUsdt':1000000,'_sellVolume24hUsdt':1000000}
    result,report=scanner._fetch_direct_route_once_local(pair,sdk_config())
    assert (result is not None) is allowed
    if allowed:assert result['status'] is False


def test_setting_roundtrip_and_unrelated_save_preserves_exception(isolated):
    scanner.update_astro_spread_subscriptions(['gateFuture','bybitFuture'],ff_bybit_sell_exception_enabled=False)
    assert not sdk.astro_ff_bybit_sell_exception_enabled()
    scanner.update_astro_spread_subscriptions(['gateFuture','bybitFuture'],ff_bybit_sell_exception_enabled=True)
    scanner.update_astro_spread_subscriptions(['gateFuture','bybitFuture'],min_volume_usdt=123)
    assert sdk.astro_ff_bybit_sell_exception_enabled()
    assert scanner.astro_spread_scanner_status()['autoCardRules']['ff']['bybitSellException']['enabled']
    with pytest.raises(ValueError):scanner.update_astro_spread_subscriptions(['gateFuture'],ff_bybit_sell_exception_enabled='yes')


@pytest.mark.parametrize('change',['threshold','switch'])
def test_submit_rechecks_threshold_and_live_switch(monkeypatch,isolated,change):
    from test_astro_sdk import fresh_quote_report
    monkeypatch.setattr(sdk,'_refresh_pending_submission_routes',lambda:None)
    monkeypatch.setattr(sdk,'_pending_submission_routes',set())
    monkeypatch.setattr(sdk,'_log',lambda *a,**kw:None)
    class Client:
        def list_pairs(self):return []
        def add_pair(self,pair):pytest.fail('must not submit after threshold/switch changes')
    pair={'name':'ABC','type':'FF','buyEx':'gate','sellEx':'bybit','openPosition':'0.15','status':False,'disableOpen':False}
    def validate(p,c):
        if change=='switch':isolated.write_text(json.dumps({'ffBybitSellExceptionEnabled':False}))
        return {**p,'openPosition':'0.1' if change=='threshold' else '0.15'},fresh_quote_report()
    assert not sdk._sync_candidate_pair(Client(),pair,sdk_config(),validate,set())
