from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app import funding_prediction_review as review
from app import funding_watch_health as health
from app.models import CryptoFundingFormationWatchItem
from test_funding_prediction_review import make_session, prediction_payload


def watch(db, now):
    item = CryptoFundingFormationWatchItem(exchange="bn", symbol="PROM", created_at=now)
    db.add(item)
    health.reset_health(db, item, now)
    db.commit()
    return item


def sample(end, now):
    return {**prediction_payload(settlement_time=end, minutes_to_funding=(end-now).total_seconds()/60),
            "updatedAt": now}


def test_new_watch_skips_past_checkpoints_and_reports_next():
    db = make_session(); now = datetime(2026, 9, 13, 0, 34, tzinfo=timezone.utc)
    w = watch(db, now); end = now.replace(hour=4, minute=0)
    health.update_health(db, w, sample(end, now), None, now); db.commit()
    result = health.health_overview(db, w, now)
    assert result["status"] == "waiting"
    assert result["nextCheckpointMinutes"] == 180
    assert result["nextCheckpointAt"] == now.replace(hour=1, minute=0).isoformat()
    assert result["missedCount"] == 0
    db.close()


def test_upgrade_initializes_existing_watch_with_production_autoflush_disabled():
    db=make_session();now=datetime.now(timezone.utc)
    w=CryptoFundingFormationWatchItem(exchange="bn",symbol="PROM",created_at=now-timedelta(days=2))
    db.add(w);db.commit()
    health.update_health(db,w,sample(now+timedelta(hours=4),now),None,now);db.commit()
    assert health._state(db,w)["startedAt"]==now.isoformat()
    assert health.health_overview(db,w,now)["status"]=="capturing"
    db.close()


def test_missed_checkpoint_only_after_grace_once_and_survives_session(monkeypatch):
    db = make_session(); now = datetime(2026, 9, 13, 0, 34, tzinfo=timezone.utc)
    w = watch(db, now); end = now.replace(hour=4, minute=0); events=[]
    monkeypatch.setattr(health, "append_system_runtime_event", lambda *a, **kw: events.append((a,kw)))
    health.update_health(db, w, sample(end, now), None, now); db.commit()
    during=now.replace(hour=1,minute=1,second=30)
    health.update_health(db, w, sample(end,during), None, during); db.commit()
    assert events == []
    late=during+timedelta(seconds=1)
    health.update_health(db, w, sample(end,late), None, late); db.commit()
    assert len(events)==1
    db.expire_all()
    health.update_health(db, w, sample(end,late), None, late); db.commit()
    result=health.health_overview(db,w,late)
    assert result["status"]=="missed" and result["missedCount"]==1
    assert len(events)==1
    db.close()


def test_recorded_checkpoint_not_reported_missed_and_cycle_rollover_keeps_misses(monkeypatch):
    db=make_session(); now=datetime(2026,9,13,3,44,tzinfo=timezone.utc)
    w=watch(db,now);end=now.replace(hour=4,minute=0)
    monkeypatch.setattr(health,"append_system_runtime_event",lambda *a,**kw:None)
    health.update_health(db,w,sample(end,now),None,now)
    assert review.record_prediction_checkpoint(db,sample(end,now),captured_at=now)
    db.commit()
    later=end+timedelta(minutes=2)
    health.update_health(db,w,sample(end+timedelta(hours=4),later),None,later);db.commit()
    result=health.health_overview(db,w,later)
    assert result["missedCount"]==1
    assert result["missedCheckpoints"][0]["minutes"]==5
    assert result["lastRecordedAt"]==now.isoformat()
    db.close()


def test_reenable_resets_expectations_but_refresh_does_not():
    db=make_session();now=datetime.now(timezone.utc);w=watch(db,now)
    end=now+timedelta(hours=4)
    health.update_health(db,w,sample(end,now),None,now);db.commit()
    original=health._state(db,w)
    review.sync_funding_formation_watches(db,[{"exchange":"bn","symbol":"PROM"}])
    assert health._state(db,w)==original
    review.sync_funding_formation_watches(db,[])
    review.sync_funding_formation_watches(db,[{"exchange":"bn","symbol":"PROM"}])
    assert health._state(db,w)["expected"]==[]
    db.close()


def test_upstream_partial_error_and_stopped_scheduler_never_look_healthy():
    db=make_session();now=datetime.now(timezone.utc);w=watch(db,now);end=now+timedelta(hours=4)
    health.update_health(db,w,sample(end,now),None,now);db.commit()
    assert health.health_overview(db,w,now+timedelta(seconds=181))["status"]=="stale"
    health.update_health(db,w,{**sample(end,now),"status":"partial_error","stale":True,"errorMessage":"upstream timeout"},None,now)
    db.commit();h=health.health_overview(db,w,now)
    assert h["status"]=="error" and h["error"]=="upstream timeout"
    db.close()


def test_pending_first_scan_becomes_stale():
    db=make_session();now=datetime.now(timezone.utc);w=watch(db,now)
    assert health.health_overview(db,w,now)["status"]=="initializing"
    assert health.health_overview(db,w,now+timedelta(minutes=4))["status"]=="stale"
    db.close()


def test_settlement_schedule_change_cancels_superseded_expectations():
    db=make_session();now=datetime(2026,9,13,0,34,tzinfo=timezone.utc);w=watch(db,now)
    end=now.replace(hour=4,minute=0)
    health.update_health(db,w,sample(end,now),None,now);db.commit()
    new_end=now.replace(hour=2,minute=0)
    health.update_health(db,w,sample(new_end,now),None,now);db.commit()
    assert all(e["settlementTime"]==new_end.isoformat() for e in health._state(db,w)["expected"])
    db.close()


def test_review_annotates_old_warning_without_mutating_event(monkeypatch):
    db=make_session();end=datetime.now(timezone.utc)-timedelta(hours=1)
    payload=prediction_payload(settlement_time=end,predicted_rate=-0.05)
    row=review.record_prediction_checkpoint(db,payload);db.commit()
    review.reconcile_pending_predictions(db,lambda *_:[SimpleNamespace(funding_time=end,funding_rate=-0.02)],
                                         now=end+timedelta(minutes=2),emit_runtime_logs=False)
    event={"at":end.isoformat(),"message":"原始未命中", "details":{
        "exchange":"bn","symbol":"PROM","settlementTime":end.isoformat(),"checkpointMinutes":15,"success":False,"absoluteError":0.03}}
    monkeypatch.setattr(review,"system_runtime_logs_overview",lambda *a,**kw:{"items":[event]})
    overview=review.funding_prediction_review_overview(db,model_version="all")
    item=overview["items"][0]
    assert overview["correctedLogCount"]==1
    assert item["scoreCorrected"] and item["success"]
    assert item["settlementPredictedRate"]==-0.02
    assert item["systemPredictedRate"]==-0.05
    assert event["details"]["success"] is False and event["message"]=="原始未命中"
    db.close()


def test_review_excludes_missing_or_invalid_bounds_from_accuracy_and_counts_beyond_details(monkeypatch):
    db=make_session();now=datetime.now(timezone.utc)
    monkeypatch.setattr(review,"system_runtime_logs_overview",lambda *a,**kw:{"items":[]})
    for i,(lo,hi) in enumerate([(-0.02,0.02),(None,None),(-0.02,None),(0.02,-0.02)]):
        end=now-timedelta(hours=i+1)
        payload=prediction_payload(settlement_time=end,predicted_rate=0.001)
        payload.update(effectiveFundingFloor=lo,effectiveFundingCap=hi)
        row=review.record_prediction_checkpoint(db,payload);db.commit()
        review._apply_evaluation(row,actual_rate=0.001,actual_time=end,evaluated_at=now);db.commit()
    o=review.funding_prediction_review_overview(db,model_version="all",limit=1)
    assert o["settledCount"]==4 and o["scoredCount"]==1 and o["legacyCount"]==3
    assert o["hitRate"]==1 and o["legacyHitRate"]==1
    assert len(o["items"])==1 and o["totalRecordCount"]==4
    assert sum(b["sampleCount"] for b in o["breakdown"])==1
    db.close()
