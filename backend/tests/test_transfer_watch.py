from app import transfer_watch as watch


def snapshot(deposit=True, withdraw=True, status="ok"):
    return dict(status=status,chains=[dict(chain="ETH",depositEnabled=deposit,withdrawEnabled=withdraw)])


def test_initial_unknown_and_unchanged_do_not_alert():
    assert watch.changes({},snapshot(False)) == []
    assert watch.changes(snapshot(),snapshot()) == []
    assert watch.changes(snapshot(),snapshot(False,status="error")) == []
    assert watch.changes(snapshot(),snapshot(None,None)) == []
    assert watch.changes(snapshot(None,None),snapshot(False,False)) == []


def test_pause_and_restore_each_chain_independently():
    assert watch.changes(snapshot(),snapshot(False)) == [dict(chain="ETH",kind="depositEnabled",before=True,after=False)]
    assert watch.changes(snapshot(False,False),snapshot()) == [dict(chain="ETH",kind="depositEnabled",before=False,after=True),dict(chain="ETH",kind="withdrawEnabled",before=False,after=True)]
    multi=snapshot(False);multi['chains'].append(dict(chain="BSC",depositEnabled=True,withdrawEnabled=True))
    assert len(watch.changes(snapshot(),multi)) == 1


def test_persistent_baseline_failure_dedup_ack_and_stop(tmp_path,monkeypatch):
    monkeypatch.setattr(watch,"get_data_dir",lambda:tmp_path)
    items=[dict(symbol="SOPH",selectedExchanges=["gt"])]
    watch.sync(items)
    watch.scan(lambda *args:snapshot())
    assert watch.overview()['recentEvents']==[]
    watch.scan(lambda *args:snapshot(None,None,status="error"))
    watch.scan(lambda *args:snapshot(False))
    events=watch.overview()['recentEvents']
    assert len(events)==1
    watch.sync(items)
    watch.scan(lambda *args:snapshot(False))
    assert len(watch.overview()['recentEvents'])==1
    watch.acknowledge([events[0]['id']])
    assert watch.overview()['recentEvents'][0]['pushed']
    watch.scan(lambda *args:snapshot())
    watch.sync([])
    assert watch.overview()['recentEvents'][0]['suppressed']
    watch.sync(items)
    watch.scan(lambda *args:snapshot(False))
    assert len(watch.overview()['recentEvents'])==2


def test_invalid_sync_keeps_previous_routes(tmp_path,monkeypatch):
    monkeypatch.setattr(watch,"get_data_dir",lambda:tmp_path)
    watch.sync([dict(symbol="SOPH",selectedExchanges=["gt"])])
    try:watch.sync([dict(symbol="BAD/COIN",selectedExchanges=["gt"])])
    except ValueError:pass
    else:raise AssertionError('invalid symbol accepted')
    assert len(watch.overview()['items'])==1


def test_bridge_bark_only_new_changes_and_ack_after_success(tmp_path,monkeypatch):
    from app import transfer_watch_bridge as bridge, crypto, notifications
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import CryptoFundingCapWatchItem
    engine=create_engine('sqlite:///:memory:')
    CryptoFundingCapWatchItem.__table__.create(engine)
    with Session(engine) as db:
        db.add(CryptoFundingCapWatchItem(symbol='SOPH',enabled=True))
        db.commit()
    monkeypatch.setattr(bridge,'SessionLocal',lambda:Session(engine))
    monkeypatch.setattr(bridge,'get_data_dir',lambda:tmp_path)
    monkeypatch.setattr(bridge,'funding_cloud_enabled',lambda:True)
    monkeypatch.setattr(crypto,'funding_cap_watch_item_exchanges',lambda r:['gt'])
    event=dict(id=1,symbol='SOPH',exchange='gt',changes=[dict(chain='ETH',kind='depositEnabled',before=True,after=False)],pushed=False)
    calls=[]
    def rpc(method,path,**kwargs):
        if path.endswith('/ack'):
            calls.append('ack');event['pushed']=True
        return dict(status='ok',items=[],recentEvents=[dict(event)])
    monkeypatch.setattr(bridge,'funding_cloud_request',rpc)
    def bark(**kwargs):
        assert '充值暂停' in kwargs['body']
        calls.append('bark');return 'ok',None
    monkeypatch.setattr(notifications,'send_bark_or_log',bark)
    bridge.refresh();bridge.refresh()
    assert calls==['bark','ack']
    event['pushed']=False
    # Cloud acknowledgement lost: successful delivery prevents another Bark.
    bridge.refresh()
    assert calls==['bark','ack','ack']
    event['id']=2
    event['pushed']=False
    monkeypatch.setattr(notifications,'send_bark_or_log',lambda **kwargs:('error','unavailable'))
    bridge.refresh()
    assert calls==['bark','ack','ack']
    assert not event['pushed']


def test_gate_current_public_fields_preserve_zero_and_pause(monkeypatch):
    from app import crypto
    import httpx
    response=httpx.Response(200,request=httpx.Request('GET','https://example.test'),json=[
        dict(chain='ETH',is_disabled=0,is_deposit_disabled=0,is_withdraw_disabled=1),
        dict(chain='BSC',is_disabled=1,is_deposit_disabled=0,is_withdraw_disabled=0)])
    monkeypatch.setattr(crypto,'rate_limited_get',lambda *a,**k:response)
    data=crypto.transfer_status_to_out(crypto.fetch_gate_transfer_status(None,'SOPH'))
    assert data['chains'][0]['depositEnabled'] is True
    assert data['chains'][0]['withdrawEnabled'] is False
    assert data['chains'][1]['depositEnabled'] is False
    assert data['chains'][1]['withdrawEnabled'] is False


def test_fast_monitor_does_not_reuse_five_minute_transfer_cache(monkeypatch):
    from app import crypto
    from datetime import datetime,timedelta,timezone
    from contextlib import nullcontext
    old=crypto.CoinTransferStatus('gt','SOPH','ok','old',True,True,[],datetime.now(timezone.utc)-timedelta(seconds=10))
    new=crypto.CoinTransferStatus('gt','SOPH','ok','new',True,False,[],datetime.now(timezone.utc))
    monkeypatch.setattr(crypto,'_coin_transfer_cache',{('gt','SOPH'):(old.updated_at,crypto.transfer_status_to_out(old))})
    monkeypatch.setattr(crypto,'http_client',lambda **kwargs:nullcontext(None))
    calls=[]
    def fetch(*args):calls.append(1);return new
    monkeypatch.setattr(crypto,'fetch_gate_transfer_status',fetch)
    assert crypto.fetch_coin_transfer_status('gt','SOPH').withdraw_enabled is True
    assert calls == []
    assert crypto.fetch_coin_transfer_status('gt','SOPH',cache_seconds=5).withdraw_enabled is False
    assert calls == [1]


def test_fast_monitor_refreshes_binance_inner_config_cache(monkeypatch):
    from app import crypto
    from datetime import datetime,timedelta,timezone
    monkeypatch.setattr(crypto,'_binance_transfer_config_cache',(datetime.now(timezone.utc)-timedelta(seconds=10),{'SOPH':{'old':True}}))
    calls=[]
    def fetch(*args):calls.append(1);return [{'coin':'SOPH','new':True}]
    monkeypatch.setattr(crypto,'signed_binance_margin_get',fetch)
    assert crypto.fetch_binance_transfer_config(None)['SOPH']['old']
    assert calls == []
    assert crypto.fetch_binance_transfer_config(None,cache_seconds=5)['SOPH']['new']
    assert calls == [1]
