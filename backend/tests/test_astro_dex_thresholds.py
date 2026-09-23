import time
import pytest
from app import astro_spread_scanner as s
from app import astro_sdk as sdk
from test_astro_sf_dex_rules import candidate
from test_astro_spread_scanner import sdk_config

@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setenv('ASTRO_SPREAD_SUBSCRIPTIONS_FILE',str(tmp_path/'settings.json'))
    monkeypatch.setattr(s,'_record_revalidation_outcome',lambda *a,**k:None)
    s.update_astro_spread_subscriptions(['okxdexSpot','pancakeswapv3Spot','gateFuture'],sf_min_open_spread_pct=.8,
        sf_okxdex_min_open_spread_pct=1.5,sf_pancakeswap_v3_min_open_spread_pct=1.5,
        sf_pancakeswap_v3_auto_card_enabled=True)

@pytest.mark.parametrize('venue',['okxdex','pancakeswapv3'])
@pytest.mark.parametrize('value,allowed',[(1.4999,False),(1.5,False),(1.5001,True)])
def test_discovery_and_three_quote_final_strict_boundary(venue,value,allowed,monkeypatch):
    c={**candidate(venue),'openSpreadPct':value,'dexMapping':{'status':'confirmed'}}
    assert s._meets_auto_card_rule(c) is allowed
    assert s._candidate_rule_decision(c)['eligible'] is allowed
    assert s._keep_scanned_candidate(c,retained_routes=set(),ff_min_open_pct=.8,sf_min_open_pct=.8,
        structure_history_enabled=False,structure_min_open_pct=.8) is allowed
    pair={'name':'ABC','type':'SF','buyEx':venue,'sellEx':'gate','openPosition':str(value/100),
          '_hotDirectHit':{'verifiedAtMs':int(time.time()*1000),'report':{'latestOpenSpreadPct':value}}}
    clock=[100.0]
    monkeypatch.setattr(s.time,'time',lambda:clock[0])
    monkeypatch.setattr(s.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    monkeypatch.setattr(s,'_fetch_direct_route_once',lambda *a,**k:(pair,{
        'reason':'eligible','latestOpenSpreadPct':value,
        'okxdexExecutablePreflight':{'source':'test'},
        'buyQuote':{'timestamp':clock[0]*1000},'sellQuote':{'timestamp':clock[0]*1000}}))
    assert (s.revalidate_astro_hot_direct_hit(pair,sdk_config())[0] is not None) is allowed
    monkeypatch.setattr(s,'_dex_mapping_confirmation',lambda c:{'status':'confirmed'})
    assert s.astro_spread_pair_submit_guard(pair)[0] is allowed


def test_independent_values_save_and_unrelated_update_preserves_them():
    s.update_astro_spread_subscriptions(['okxdexSpot','pancakeswapv3Spot','gateFuture'],
        sf_okxdex_min_open_spread_pct=2.0,sf_pancakeswap_v3_min_open_spread_pct=1.7)
    s.update_astro_spread_subscriptions(['okxdexSpot','pancakeswapv3Spot','gateFuture'],min_volume_usdt=12345)
    assert s.spread_scan_sf_route_min_open_pct('okxdex')==2.0
    assert s.spread_scan_sf_route_min_open_pct('pancakeswapv3')==1.7
    assert s.spread_scan_sf_route_min_open_pct('binance')==.8
    values=s.astro_spread_scanner_status()['autoCardRules']['sf']['dexMinOpenSpreadPctExclusive']
    assert values=={'okxdex':2.0,'pancakeswapv3':1.7}


@pytest.mark.parametrize('value',[0,-1,float('nan'),float('inf'),101])
def test_invalid_threshold_does_not_save(value):
    before=s._read_saved_subscription_payload()
    with pytest.raises(ValueError):s.update_astro_spread_subscriptions(['okxdexSpot'],sf_okxdex_min_open_spread_pct=value)
    assert s._read_saved_subscription_payload()==before


def test_pancake_retains_chain_config_without_display_note():
    c={**candidate('pancakeswapv3'),'dexMapping':{'status':'confirmed'}}
    pair=sdk.build_astro_spread_pair(c,sdk_config())
    assert pair['_dexConfig']['chainIndex']=='56'
    assert '_chainNote' not in pair
    other=sdk.build_astro_spread_pair({**c,'buyExchange':'okxdex'},sdk_config())
    assert other['_chainNote'].startswith('OKXDEX链：')
