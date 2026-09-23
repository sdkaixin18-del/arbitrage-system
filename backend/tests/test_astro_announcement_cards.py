import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pytest
from app import astro_news_policy as news, astro_sdk as sdk, astro_spread_scanner as scanner, astro_card_registry as registry
from test_astro_news_policy import notice, payload, iso
from test_astro_sdk import config


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(news, "_completed_listing_events", set())
    monkeypatch.setenv('ASTRO_AUTO_CARD_REGISTRY_FILE',str(tmp_path/'registry.json'))
    monkeypatch.setenv('ASTRO_SPREAD_SUBSCRIPTIONS_FILE',str(tmp_path/'subscriptions.json'))
    for name,value in [('_blocks',{}),('_listings',{}),('_trading_markets',{}),('_state',{}),('_path',tmp_path/'news.json'),('_startup_pending',False),('_storage_loaded',True)]:
        monkeypatch.setattr(news,name,value)
    monkeypatch.setattr(scanner,'spread_scan_market_keys',lambda: {'binanceSpot','binanceFuture','gateSpot','gateFuture','bitgetFuture'})
    monkeypatch.setattr(scanner,'_active_delisting_exchange_blocks',lambda: set())
    monkeypatch.setattr(scanner,'_load_pulse_symbol_aliases',lambda: {})
    monkeypatch.setattr(scanner,'_route_block_match',lambda **k: None)
    monkeypatch.setattr(sdk,'astro_route_dedupe_state',lambda p: None)


def listing(ex='bn',mk='contract',**changes):
    domains={'bn':'binance.com','gt':'gate.com','bg':'bitget.com'}
    return notice(symbol='XYZ',exchange=ex,marketType=mk,action='listing',announcementUrl=f'https://{domains[ex]}/announcement/xyz',**changes)


def quotes(ex='gate',mk='future',coin='XYZ',age=0):
    key=ex+('Future' if mk=='future' else 'Spot')
    return {'data':{key:{'ts':(time.time()-age)*1000,'list':[{'name':coin,'a':1,'b':.99}]}}}


def test_both_announced_build_sf_and_both_ff_directions_before_markets_exist():
    news.ingest(payload(listing('bn','spot'), listing('bn'),listing('gt')))
    routes=news.current_listing_routes()
    assert {(r['type'],r['buyExchange'],r['sellExchange']) for r in routes} == {
        ('SF','binance','binance'),('SF','binance','gate'),('FF','binance','gate'),('FF','gate','binance')}
    assert all(r['category']=='both_announced' for r in routes)
    assert news._trading_markets == {}


def test_live_plus_unlisted_announced_needs_no_other_announcement():
    news.ingest(payload(listing('bn')))
    news.update_trading_markets([quotes(),quotes(mk='spot')])
    routes=news.current_listing_routes()
    assert {(r['type'],r['buyExchange'],r['sellExchange']) for r in routes}=={
        ('FF','binance','gate'),('FF','gate','binance'),('SF','gate','binance')}
    assert all(r['category']=='live_and_announced' for r in routes)


def test_unannounced_nontrading_market_never_invented():
    news.ingest(payload(listing('bn')))
    assert news.current_listing_routes()==[]
    news.update_trading_markets([quotes(age=90)])
    assert news.current_listing_routes()==[]
    news.update_trading_markets([quotes(coin='DIFFERENT')])
    assert news.current_listing_routes()==[]


def test_later_launch_old_publication_and_feed_rotation_survive_restart():
    news.ingest(payload(listing(publishedAt=iso(time.time()-3*86400),scheduledAt=iso(time.time()+86400))))
    news.ingest(payload())
    assert news.active_listings()
    path=news._path;news._listings={};news.configure(path)
    assert news.active_listings()


def test_paused_cards_skip_depth_funding_price_checks_and_have_explicit_defaults(monkeypatch):
    news.ingest(payload(listing('bn'),listing('gt')))
    calls=[]
    def schedule(pairs,cfg,**kwargs):
        calls.append((pairs,kwargs)); return {'state':'queued'}
    monkeypatch.setattr(sdk,'schedule_astro_pairs',schedule)
    monkeypatch.setattr(scanner,'_fetch_direct_route_once',lambda *a,**k:pytest.fail('No price check for announced paused cards'))
    monkeypatch.setattr(scanner,'_hot_funding_precheck',lambda *a,**k:pytest.fail('No funding check for announcement cards'))
    monkeypatch.setattr(scanner,'spread_scan_ff_min_open_pct',lambda: .75)
    news.schedule_listing_cards(config())
    pairs,options=calls[0]
    assert len(pairs)==2 and options.get('revalidator') is None
    assert options['submit_guard'] is news.announcement_submit_guard
    for p in pairs:
        assert p['status'] is False and p['disableOpen'] is False
        assert p['openPosition']=='0.0075'
        assert news.announcement_submit_guard(p)[0]
        assert not news.announcement_submit_guard({**p,'status':True})[0]


def test_delisting_and_manual_blocks_still_win_at_final_submission(monkeypatch):
    news.ingest(payload(listing('bn'),listing('gt')))
    pair={'name':'XYZ','type':'FF','buyEx':'binance','sellEx':'gate','status':False,'disableOpen':False,'_announcementCard':{}}
    assert news.announcement_submit_guard(pair)[0]
    monkeypatch.setattr(scanner,'_route_block_match',lambda **k:{'reason':'manual_block'})
    assert not news.announcement_submit_guard(pair)[0]
    monkeypatch.setattr(scanner,'_route_block_match',lambda **k:None)
    news.ingest(payload({**listing('gt'),'action':'delisting'}))
    assert not news.announcement_submit_guard(pair)[0]


def test_pending_or_existing_routes_are_not_resubmitted(monkeypatch):
    news.ingest(payload(listing('bn'),listing('gt')))
    monkeypatch.setattr(sdk,'astro_route_dedupe_state',lambda p:'submission_pending')
    monkeypatch.setattr(sdk,'schedule_astro_pairs',lambda *a,**k:pytest.fail('duplicate submission'))
    news.schedule_listing_cards(config())
    assert all(r['state']=='submission_pending' for r in news._state['listingRoutes'])


def test_announcement_marker_survives_unknown_submission_and_protects_cleanup():
    pair={'name':'XYZ','type':'FF','buyEx':'binance','sellEx':'gate','status':False,'disableOpen':False,'openPosition':'.0075','_announcementCard':{'sources':['https://binance.com/announcement/xyz']}}
    registry.record_pending_astro_submission(pair,'awaiting_confirmation')
    remote={k:v for k,v in pair.items() if not k.startswith('_')};remote['id']='abcdefghij'
    registry.reconcile_pending_astro_submissions([remote])
    row=registry.auto_created_route_records()[0]
    assert row['announcementPrecreated'] is True
    now=datetime.now(timezone.utc)+timedelta(days=2)
    observations={('XYZ','FF','binance','gate'):{'state':'invalid','reason':'spread_below_threshold'}}
    assert registry.observe_auto_card_cleanup(observations,[remote],now=now,grace_seconds=0,invalid_seconds=0)==[]
    assert registry.observe_auto_card_cleanup(observations,[remote],now=now+timedelta(seconds=90),grace_seconds=0,invalid_seconds=0)==[]


def test_normal_confirmation_protects_cleanup_and_sdk_omits_local_marker():
    pair={'name':'XYZ','type':'FF','buyEx':'binance','sellEx':'gate','status':False,'disableOpen':False,'openPosition':'.0075','_announcementCard':{'category':'both_announced'}}
    remote={k:v for k,v in pair.items() if not k.startswith('_')};remote['id']='abcdefghij'
    registry.register_auto_created_pair(pair,astro_pair=remote)
    assert registry.auto_created_route_records()[0]['announcementPrecreated']
    import httpx
    bodies=[]
    def handler(request):
        bodies.append(json.loads(request.content));return httpx.Response(200,json={'code':0})
    with sdk.AstroSdkClient(config(),transport=httpx.MockTransport(handler)) as client:
        client.add_pair(pair)
    assert bodies[0]['pair']=={k:v for k,v in pair.items() if not k.startswith('_')}


def test_full_submission_accepts_no_market_price_and_confirms_paused_card(monkeypatch, tmp_path):
    import httpx
    monkeypatch.setenv('STOCK_REVIEW_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(sdk,'_pending_submission_routes',set())
    monkeypatch.setattr(sdk,'_log',lambda *a,**k:None)
    news.ingest(payload(listing('bn'),listing('gt')))
    route=news.current_listing_routes()[0]
    pair=sdk.build_astro_spread_pair({**route,'openSpreadPct':.75},config())
    pair['_announcementCard']={'category':'both_announced'}
    stored=[];calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body['action'])
        if body['action']=='add':
            stored.append({**body['pair'],'id':'abcdefghij'})
            return httpx.Response(200,json={'code':0})
        assert body['action']=='list'
        return httpx.Response(200,json={'code':0,'data':deepcopy(stored)})
    with sdk.AstroSdkClient(config(),transport=httpx.MockTransport(handler)) as client:
        assert sdk._sync_candidate_pair(client,pair,config(),None,set(),news.announcement_submit_guard)
    assert calls.count('add')==1
    assert stored[0]['status'] is False and stored[0]['disableOpen'] is False
    assert registry.auto_created_route_records()[0]['announcementPrecreated']


def test_announcement_new_cards_have_listing_note(monkeypatch):
    news.ingest(payload(listing('bn'),listing('gt')))
    captured=[]
    monkeypatch.setattr(sdk,'schedule_astro_pairs',lambda pairs,*a,**k: captured.extend(pairs) or {})
    news.schedule_listing_cards(config())
    assert captured and all(p['_chainNote']=='上架' for p in captured)
    ordinary=sdk.build_astro_spread_pair({'symbol':'ORDINARY','type':'FF','buyExchange':'binance','sellExchange':'gate','openSpreadPct':1},config())
    assert '_chainNote' not in ordinary


def test_listing_note_backfill_targets_only_confirmed_owned_cards_and_retries(monkeypatch):
    card={'id':'test-card','name':'XYZ','type':'FF','buyEx':'binance','sellEx':'gate','status':False}
    row={**card,'astroPairId':'test-card','announcementPrecreated':True}
    monkeypatch.setattr(registry,'auto_created_route_records',lambda:[row])
    monkeypatch.setattr(news,'_label_published_ids',set())
    calls=[];succeed=[False]
    def publish(pair,actual):
        calls.append((pair,actual));return succeed[0]
    monkeypatch.setattr(sdk,'_publish_astro_chain_label',publish)
    news.ensure_announcement_labels([card,{**card,'id':'ordinary-card'}])
    assert len(calls)==1 and not news._label_published_ids
    succeed[0]=True
    news.ensure_announcement_labels([card])
    news.ensure_announcement_labels([card])
    assert len(calls)==2 and calls[-1][0]['_chainNote']=='上架'
    assert calls[-1][1]==card


def test_passed_launch_does_not_precreate_even_with_old_two_hour_expiry():
    news.ingest(payload(listing(scheduledAt=iso(time.time()-1))))
    assert news.active_listings()==[]


def test_reported_completed_event_overrides_future_announcement():
    news.ingest(payload(listing(hasOccurred=True)))
    assert news.active_listings()==[]


def test_unknown_launch_already_trading_is_retired_and_cannot_replay(monkeypatch):
    monkeypatch.setattr(news,'_completed_listing_events',set())
    data=payload(listing(scheduledAt=None))
    news.ingest(data)
    assert news.active_listings()
    news.update_trading_markets([quotes(ex='binance')])
    assert news.active_listings()==[]
    news.ingest(data)
    assert news.active_listings()==[]
    path=news._path;news._completed_listing_events=set();news.configure(path)
    news.ingest(data)
    assert news.active_listings()==[]


def test_already_listed_title_and_future_new_event_same_coin(monkeypatch):
    monkeypatch.setattr(news,'_completed_listing_events',set())
    old=listing(scheduledAt=None,announcementTitle='Gate 已上线 XYZ 合约交易')
    news.ingest(payload(old))
    assert news.active_listings()==[]
    new={**listing(),'announcementUrl':'https://binance.com/announcement/new-listing'}
    news.ingest(payload(new))
    assert len(news.active_listings())==1


@pytest.mark.parametrize('title', ['Bitget 现货杠杆新增 CP/USDT, SOPH/USDT！', 'New Margin Trading Pairs: CP/USDT', '新增 XYZ 杠杆借贷服务'])
def test_service_expansion_is_not_a_listing_trigger(title):
    news.ingest(payload(listing('gt', 'spot', announcementTitle=title), listing('bn')))
    assert all(r['exchange'] != 'gate' for r in news.active_listings())
    assert news.current_listing_routes() == []


def test_combined_real_listing_with_margin_service_is_kept():
    news.ingest(payload(listing('gt', 'spot', announcementTitle='Gate 将上线 XYZ 现货交易、杠杆借贷交易')))
    assert len(news.active_listings()) == 1


@pytest.mark.parametrize('changes', [
    {'contractKind': 'delivery'},
    {'announcementTitle': '币安合约将上线U本位和币本位次季0326交割合约'},
    {'announcementTitle': 'Binance Will Launch BTC Quarterly Delivery Futures'},
])
def test_dated_delivery_contract_does_not_trigger_listing_cards(changes):
    news.ingest(payload(listing('bn', **changes)))
    assert news.active_listings() == []
    assert news.current_listing_routes() == []


def test_perpetual_contract_listing_is_not_mistaken_for_delivery():
    news.ingest(payload(listing('bn', announcementTitle='币安将上线 XYZUSDT 永续合约')))
    assert len(news.active_listings()) == 1


def test_ingest_after_live_scan_does_not_reintroduce_completed_market():
    news.update_trading_markets([quotes(ex='binance'), quotes()])
    news.ingest(payload(listing('bn', scheduledAt=None)))
    assert news.active_listings() == []
    assert news.current_listing_routes() == []
    news._trading_markets = {}
    news.ingest(payload(listing('bn', scheduledAt=None)))
    assert news.active_listings() == []


def test_completed_article_language_variants_and_storage_migration(tmp_path):
    old={**listing('gt', 'spot', scheduledAt=None, hasOccurred=True),
         'announcementUrl':'https://www.gate.com/announcements/article/101583'}
    news.ingest(payload(old))
    # Mimic the exact legacy key representation on disk.
    saved=json.loads(news._path.read_text())
    saved['completedListings']=[json.dumps(['XYZ','gate','spot',old['announcementUrl']])]
    news._path.write_text(json.dumps(saved))
    news.configure(news._path)
    variant={**old, 'hasOccurred':False, 'announcementUrl':'https://www.gate.com/zh/announcements/article/101583?utm_source=feed'}
    news.ingest(payload(variant))
    assert news.active_listings() == []
    variant['announcementUrl']='https://www.gate.com/zh/announcements/article/101999'
    variant['scheduledAt']=iso(time.time()+3600)
    news.ingest(payload(variant))
    assert len(news.active_listings()) == 1


def test_article_query_ids_are_not_collapsed():
    first={'symbol':'XYZ','exchange':'gate','market':'spot','sourceUrl':'https://gate.com/announcement?id=1'}
    assert news._listing_event_key(first) != news._listing_event_key({**first,'sourceUrl':'https://gate.com/announcement?id=2'})
