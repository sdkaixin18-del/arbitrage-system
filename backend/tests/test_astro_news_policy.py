from copy import deepcopy
from datetime import datetime, timezone
import time
import json
import pytest
from app import astro_news_policy as news
from app import astro_spread_scanner as scanner


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(news, "_completed_listing_events", set())
    monkeypatch.setattr(news, '_storage_loaded', True)
    monkeypatch.setattr(news, '_blocks', {})
    monkeypatch.setattr(news, '_listings', {})
    monkeypatch.setattr(news, '_path', tmp_path/'state.json')
    monkeypatch.setattr(news, '_state', {'storageError': None})
    monkeypatch.setattr(scanner, '_append_decision_audit', lambda *a, **kw: None)


def notice(**changes):
    return {'symbol': '4', 'exchange': 'bg', 'marketType': 'contract', 'action': 'delisting',
            'announcementMatched': True, 'announcementUrl': 'https://www.bitget.com/zh-CN/support/articles/12560603894224',
            'publishedAt': iso(time.time()-60), 'scheduledAt': iso(time.time()+3600), **changes}


def payload(*rows):
    return {'updatedAt': iso(time.time()), 'announcements': [], 'listingReminders': list(rows)}


def test_numeric_symbol_reminder_outside_capped_news_list():
    news.ingest(payload(notice()))
    assert news.route_check('4', 'FF', 'binance', 'bitget')['exchange'] == 'bitget'
    assert news.route_check('4', 'FF', 'bitget', 'gate')
    assert news.route_check('4', 'SF', 'bitget', 'gate') is None
    assert news.route_check('4', 'SF', 'gate', 'bitget')
    assert news.route_check('4', 'FF', 'binance', 'gate') is None
    assert news.route_check('14', 'FF', 'binance', 'bitget') is None


def test_spot_only_does_not_block_ff():
    news.ingest(payload(notice(marketType='spot')))
    assert news.route_check('4', 'SF', 'bitget', 'gate')
    assert news.route_check('4', 'FF', 'bitget', 'gate') is None
    assert news.route_check('4', 'FS', 'gate', 'bitget')


@pytest.mark.parametrize('changes', [dict(announcementMatched=False), dict(marketType='unknown'),
    dict(announcementUrl='https://bitget.com.fake.example/notice'), dict(symbol=None)])
def test_ambiguous_untrusted_news_does_not_invent_blocks(changes):
    news.ingest(payload(notice(**changes)))
    assert not news._blocks
    assert news._state['unresolvedNoticeCount'] == 1


def test_persistence_feed_rotation_and_restart():
    news.ingest(payload(notice()))
    news.ingest(payload())
    path = news._path
    news._blocks = {}
    news.configure(path)
    assert news.route_check('4', 'FF', 'gate', 'bitget')
    stale = payload(); stale['updatedAt'] = iso(time.time()-3600)
    news.ingest(stale)
    assert news.route_check('4', 'FF', 'gate', 'bitget')
    assert news._state['lastError'] == 'news_snapshot_stale'


def test_corrupt_storage_fails_closed(tmp_path):
    path = tmp_path/'bad';path.write_text('{')
    news.configure(path)
    assert news.route_check('ABC', 'SF', 'gate', 'binance')['reason'] == 'news_block_storage_unavailable'


def test_news_final_submit_guard_independent_of_old_toggle(monkeypatch):
    news.ingest(payload(notice()))
    monkeypatch.setattr(scanner, 'spread_scan_exclude_delisted_exchange_cards', lambda: False)
    pair = {'name': '4', 'type': 'FF', 'buyEx': 'gate', 'sellEx': 'bitget'}
    assert scanner.astro_spread_pair_submit_guard(pair)[0] is False
    candidate = {'symbol': '4', 'type': 'FF', 'buyExchange': 'gate', 'sellExchange': 'bitget'}
    kept, report = scanner._filter_delisted_exchange_candidates([candidate])
    assert not kept and report['filteredCandidateCount'] == 1
    assert scanner._route_contract_restriction(candidate)['source'] == 'local_exchange_news'


def test_old_index_uses_explicit_market_scope():
    blocks = {('4', 'bitget', 'future')}
    assert scanner._indexed_delisting_match(blocks, '4', 'SF', 'bitget', 'gate') is None
    assert scanner._indexed_delisting_match(blocks, '4', 'FF', 'bitget', 'gate') == 'bitget'


class Client:
    def __init__(self, pairs, fail=False):
        self.pairs = deepcopy(pairs);self.writes=[];self.fail=fail
    def list_pairs(self, **kwargs):
        return deepcopy(self.pairs)
    def request(self, body, **kwargs):
        self.writes.append(deepcopy(body))
        if self.fail:
            raise TimeoutError()
        self.pairs = [deepcopy(body['pair']) if row['id']==body['pair']['id'] else row for row in self.pairs]


def card(**changes):
    return {'id': 'abcdefghij', 'name': '4', 'type': 'FF', 'buyEx': 'gate', 'sellEx': 'bitget',
            'disableOpen': False, 'disableClose': False, 'status': True, 'closePosition': '0.001',
            'openPosition': '0.015', 'stepClose': [{'amount': 20}], 'maxTradeUSDT': '100', **changes}


def test_existing_cards_only_disable_open_with_readback_and_no_duplicate_writes():
    news.ingest(payload(notice()))
    pair = card();client = Client([pair, card(id='unaffected', name='WOO')])
    result = news.enforce_cards(client)
    assert result[0]['status'] == 'confirmed'
    assert client.writes == [{'action': 'update', 'pair': {**pair, 'disableOpen': True}}]
    news.enforce_cards(client)
    assert len(client.writes) == 1
    assert client.pairs[1]['disableOpen'] is False


def test_unknown_write_not_claimed_success_and_retries_current_state():
    news.ingest(payload(notice()));client=Client([card()], fail=True)
    assert news.enforce_cards(client)[0]['status'] == 'pending'
    client.fail=False
    assert news.enforce_cards(client)[0]['status'] == 'confirmed'


@pytest.mark.parametrize('changes', [dict(recoveredAfterGap=True), dict(notificationPolicy='historical_only'),
 dict(publishedAt=iso(time.time()-90000), scheduledAt=iso(time.time()-80000)), dict(assetType='stock')])
def test_old_or_recovered_listing_is_not_new(changes):
    news.ingest(payload(notice(action='listing',**changes)))
    assert news.active_listings() == []


def test_stale_listing_feed_is_not_replayed():
    data=payload(notice(action='listing'));data['updatedAt']=iso(time.time()-500)
    news.ingest(data)
    assert news.active_listings() == []


def test_write_failure_retries_without_requiring_a_new_notice(monkeypatch):
    real_save=news._save
    monkeypatch.setattr(news, '_save', lambda: (_ for _ in ()).throw(OSError()))
    data=payload(notice());news.ingest(data)
    assert news._state['storageError']
    monkeypatch.setattr(news, '_save', real_save)
    news.ingest(data)
    assert news._state['storageError'] is None
    assert news._path.exists()


def test_corrupt_storage_not_overwritten_by_partial_live_feed(tmp_path):
    path=tmp_path/'bad';path.write_text('{')
    news.configure(path);news.ingest(payload(notice()))
    assert path.read_text() == '{'
    assert news._state['storageError']


def test_startup_waits_for_first_news_read(monkeypatch):
    monkeypatch.setattr(news, '_startup_pending', True)
    assert news.route_check('ABC', 'FF', 'binance', 'gate')['reason'] == 'news_policy_initializing'
