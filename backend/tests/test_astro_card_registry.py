from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import app.astro_card_registry as registry_module
import pytest

from app.astro_card_registry import (
    apply_delete_rearm_rules,
    astro_cleanup_status,
    astro_delete_rearm_status,
    mark_auto_card_system_deleted,
    observe_auto_card_cleanup,
    register_auto_created_pair,
    reset_registry_for_tests,
)


def pair(*, sell_exchange: str = "okx") -> dict[str, object]:
    return {
        "name": "RVN",
        "type": "FF",
        "buyEx": "gate",
        "sellEx": sell_exchange,
        "openPosition": "0.012",
    }


def test_config_snapshot_detaches_nested_settings_and_marks_unreadable_fields() -> None:
    submitted = {**pair(), "adjustParams": {"nested": {"enabled": False}}, "priceAlertOnlyRise": False}
    returned = {key: value for key, value in submitted.items() if key != "priceAlertOnlyRise"}
    record = registry_module._route_record(submitted, datetime.now(timezone.utc), astro_pair=returned)
    submitted["adjustParams"]["nested"]["enabled"] = True
    assert record["createdPair"]["adjustParams"]["nested"]["enabled"] is False
    assert record["createdPairSnapshotVersion"] == registry_module.AUTO_CARD_SNAPSHOT_VERSION
    assert record["submittedOnlyConfigFields"] == ["priceAlertOnlyRise"]
    assert "priceAlertOnlyRise" not in record["createdReadbackConfigFields"]


def test_cleanup_status_exposes_incomplete_snapshot_protection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    submitted = {**pair(), "priceAlertOnlyRise": False}
    register_auto_created_pair(submitted, astro_pair={**pair(), "id": "partial"})
    coverage = astro_cleanup_status()["configSnapshotCoverage"]
    assert coverage["submittedOnlyProtected"] == 1
    assert coverage["completeReadbackSnapshots"] == 0
    assert coverage["unreadableSubmittedFields"] == ["priceAlertOnlyRise"]


def test_disabled_alert_reports_inactive_unreadable_direction(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    submitted = {**pair(), "priceAlert": "", "priceAlertOnlyRise": True}
    register_auto_created_pair(submitted, astro_pair={**pair(), "id": "readback", "priceAlert": ""})
    coverage = astro_cleanup_status()["configSnapshotCoverage"]
    assert coverage["inactiveDirectionOnlySnapshots"] == 1
    assert coverage["submittedOnlyProtected"] == 0
    assert coverage["completeReadbackSnapshots"] == 0


@pytest.mark.parametrize("malformed", ["{bad", "[]", '{"routes": []}', '{"pendingSubmissions": {"bad": {}}}'])
def test_malformed_registry_never_overwrites_pending_storage(monkeypatch, tmp_path, malformed) -> None:
    path = tmp_path / "registry.json"
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(path))
    path.write_text(malformed, encoding="utf-8")
    with pytest.raises(RuntimeError):
        registry_module.record_pending_astro_submission(pair(), "submitting")
    assert path.read_text(encoding="utf-8") == malformed


def test_unreadable_registry_prevents_pending_write(monkeypatch, tmp_path) -> None:
    path = tmp_path / "registry.json"
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(path))
    registry_module.record_pending_astro_submission(pair(), "outcome_unknown")
    before = path.read_text(encoding="utf-8")
    real_read = type(path).read_text

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("test unreadable registry")
        return real_read(candidate, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(type(path), "read_text", denied)
        with pytest.raises(RuntimeError, match="注册表读取失败"):
            registry_module.record_pending_astro_submission(pair(sell_exchange="bitget"), "submitting")
    assert path.read_text(encoding="utf-8") == before


def test_deleted_auto_card_requires_confirmation_then_waits_for_pullback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    identity = ("RVN", "FF", "gate", "okx")
    register_auto_created_pair(route, now=start)

    allowed, suppressed, guarded = apply_delete_rearm_rules(
        [route], [], route_observations={identity: {"state": "eligible", "openSpreadPct": 1.8}},
        now=start + timedelta(seconds=5), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == []
    assert suppressed[0]["reason"] == "pending_confirmation"
    assert guarded == []

    allowed, suppressed, guarded = apply_delete_rearm_rules(
        [route], [], route_observations={identity: {"state": "eligible", "openSpreadPct": 2.5}},
        now=start + timedelta(seconds=10), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == []
    assert suppressed[0]["reason"] == "waiting_for_pullback_or_direct_breakout"
    assert suppressed[0]["deletionReferenceOpenPosition"] == pytest.approx(0.018)
    assert suppressed[0]["rearmOpenPosition"] == pytest.approx(0.0216)
    assert len(guarded) == 1
    status = astro_delete_rearm_status()
    assert status["activeGuardCount"] == 1
    assert status["waitingPullbackCount"] == 1
    assert status["waitingRetriggerCount"] == 0


def test_deleted_route_is_detected_even_when_current_scan_has_no_candidate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    identity = ("RVN", "FF", "gate", "okx")
    register_auto_created_pair(route, now=start)

    apply_delete_rearm_rules(
        [], [], route_observations={identity: {"state": "eligible", "openSpreadPct": 1.8}},
        now=start + timedelta(seconds=5), confirmation_seconds=5,
    )
    apply_delete_rearm_rules(
        [], [], route_observations={identity: {"state": "eligible", "openSpreadPct": 2.5}},
        now=start + timedelta(seconds=10), confirmation_seconds=5,
    )

    item = astro_delete_rearm_status()["items"][0]
    assert item["deletionReferenceOpenPosition"] == pytest.approx(0.018)
    assert item["rearmOpenPosition"] == pytest.approx(0.0216)


def test_registry_distinguishes_exact_dex_lifecycle_fingerprints(monkeypatch, tmp_path) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(registry_path))
    reset_registry_for_tests()
    base = {
        "name": "ABC",
        "type": "SF",
        "buyEx": "okxdex",
        "sellEx": "gate",
        "openPosition": "0.015",
    }
    register_auto_created_pair({
        **base,
        "_dexConfig": {"chainIndex": "1", "contractAddress": "0xABC"},
    })
    register_auto_created_pair({
        **base,
        "_dexConfig": {"chainIndex": "56", "contractAddress": "0xDEF"},
    })

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(payload["routes"]) == 2
    records = list(payload["routes"].values())
    assert {(item["dexChainIndex"], item["dexContractAddress"]) for item in records} == {
        ("1", "0xabc"),
        ("56", "0xdef"),
    }


def test_only_exact_route_is_blocked_and_direct_twenty_percent_breakout_retriggers(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    other_route = pair(sell_exchange="binance")
    register_auto_created_pair(route, now=start)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=5), rearm_pct=20, confirmation_seconds=5)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=10), rearm_pct=20, confirmation_seconds=5)

    allowed, suppressed, _ = apply_delete_rearm_rules(
        [route, other_route], [], now=start + timedelta(minutes=30), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == [other_route]
    assert suppressed[0]["reason"] == "waiting_for_pullback_or_direct_breakout"

    larger_route = {**route, "openPosition": "0.0145"}
    allowed, suppressed, _ = apply_delete_rearm_rules(
        [larger_route], [], now=start + timedelta(hours=1), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == [larger_route]
    assert suppressed == []


def test_confirmed_half_point_pullback_rearms_at_normal_threshold(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    identity = ("RVN", "FF", "gate", "okx")
    register_auto_created_pair(route, now=start)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=5), rearm_pct=20, confirmation_seconds=5)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=10), rearm_pct=20, confirmation_seconds=5)

    pullback = {
        identity: {
            "state": "invalid",
            "reason": "spread_below_ff_threshold",
            "openSpreadPct": 0.69,
        }
    }
    apply_delete_rearm_rules(
        [], [], route_observations=pullback, now=start + timedelta(seconds=15),
        rearm_pct=20, pullback_pct_points=0.5, confirmation_seconds=5,
    )
    apply_delete_rearm_rules(
        [], [], route_observations=pullback, now=start + timedelta(seconds=20),
        rearm_pct=20, pullback_pct_points=0.5, confirmation_seconds=5,
    )
    status = astro_delete_rearm_status()
    assert status["waitingPullbackCount"] == 0
    assert status["waitingRetriggerCount"] == 1

    normal_retrigger = {**route, "openPosition": "0.0101"}
    allowed, suppressed, _ = apply_delete_rearm_rules(
        [normal_retrigger], [], now=start + timedelta(seconds=25),
        rearm_pct=20, pullback_pct_points=0.5, confirmation_seconds=5,
    )
    assert allowed == [normal_retrigger]
    assert suppressed == []


def test_pullback_must_be_continuous_before_retrigger(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    identity = ("RVN", "FF", "gate", "okx")
    register_auto_created_pair(route, now=start)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=5), rearm_pct=20, confirmation_seconds=5)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=10), rearm_pct=20, confirmation_seconds=5)

    pullback = {
        identity: {
            "state": "invalid",
            "reason": "spread_below_ff_threshold",
            "openSpreadPct": 0.69,
        }
    }
    eligible = {identity: {"state": "eligible", "reason": "eligible", "openSpreadPct": 1.1}}
    apply_delete_rearm_rules(
        [], [], route_observations=pullback, now=start + timedelta(seconds=15), confirmation_seconds=5
    )
    apply_delete_rearm_rules(
        [], [], route_observations=eligible, now=start + timedelta(seconds=20), confirmation_seconds=5
    )
    apply_delete_rearm_rules(
        [], [], route_observations=pullback, now=start + timedelta(seconds=25), confirmation_seconds=5
    )
    assert astro_delete_rearm_status()["waitingPullbackCount"] == 1

    apply_delete_rearm_rules(
        [], [], route_observations=pullback, now=start + timedelta(seconds=30), confirmation_seconds=5
    )
    assert astro_delete_rearm_status()["waitingRetriggerCount"] == 1


def test_legacy_pullback_flag_without_half_point_evidence_is_cleared(monkeypatch, tmp_path) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(registry_path))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    register_auto_created_pair(route, now=start)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=5), confirmation_seconds=5)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=10), confirmation_seconds=5)

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    record = next(iter(payload["routes"].values()))
    record["rearmPullbackObservedAt"] = (start + timedelta(seconds=15)).isoformat()
    record["rearmPullbackConfirmedAt"] = (start + timedelta(seconds=20)).isoformat()
    record.pop("rearmPullbackPctPoints", None)
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    allowed, suppressed, _ = apply_delete_rearm_rules(
        [route], [], now=start + timedelta(seconds=25),
        rearm_pct=20, pullback_pct_points=0.5, confirmation_seconds=5,
    )
    assert allowed == []
    assert suppressed[0]["reason"] == "waiting_for_pullback_or_direct_breakout"
    assert astro_delete_rearm_status()["waitingPullbackCount"] == 1


def test_manually_restored_card_clears_pending_delete(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    register_auto_created_pair(route, now=start)
    apply_delete_rearm_rules([route], [], now=start + timedelta(seconds=5), rearm_pct=20, confirmation_seconds=5)

    allowed, suppressed, guarded = apply_delete_rearm_rules(
        [route], [route], now=start + timedelta(seconds=10), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == [route]
    assert suppressed == []
    assert guarded == []
    assert astro_delete_rearm_status()["pendingDeletionCount"] == 0


def test_cleanup_requires_grace_and_continuous_invalid_observations(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    astro_pair = {**route, "id": "pair-1", "status": False}
    register_auto_created_pair(route, astro_pair=astro_pair, now=start)
    observation = {
        ("RVN", "FF", "gate", "okx"): {
            "state": "invalid",
            "reason": "spread_below_ff_threshold",
        }
    }

    assert observe_auto_card_cleanup(
        observation, [astro_pair], now=start + timedelta(seconds=119), grace_seconds=120, invalid_seconds=60
    ) == []
    assert observe_auto_card_cleanup(
        observation, [astro_pair], now=start + timedelta(seconds=120), grace_seconds=120, invalid_seconds=60
    ) == []
    assert observe_auto_card_cleanup(
        observation, [astro_pair], now=start + timedelta(seconds=179), grace_seconds=120, invalid_seconds=60
    ) == []
    ready = observe_auto_card_cleanup(
        observation, [astro_pair], now=start + timedelta(seconds=180), grace_seconds=120, invalid_seconds=60
    )
    assert len(ready) == 1
    assert ready[0]["invalidForSeconds"] == 60


def test_unavailable_observation_breaks_cleanup_invalidity_window(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = pair()
    astro_pair = {**route, "id": "pair-1", "status": False}
    register_auto_created_pair(route, astro_pair=astro_pair, now=start)
    identity = ("RVN", "FF", "gate", "okx")
    invalid = {identity: {"state": "invalid", "reason": "spread_below_ff_threshold"}}
    unavailable = {identity: {"state": "unavailable", "reason": "quote_unavailable"}}

    observe_auto_card_cleanup(invalid, [astro_pair], now=start + timedelta(seconds=120), grace_seconds=120)
    observe_auto_card_cleanup(unavailable, [astro_pair], now=start + timedelta(seconds=170), grace_seconds=120)
    assert observe_auto_card_cleanup(
        invalid, [astro_pair], now=start + timedelta(seconds=180), grace_seconds=120, invalid_seconds=60
    ) == []


def test_system_cleanup_uses_cooldown_without_manual_delete_rearm(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRO_AUTO_CARD_REGISTRY_FILE", str(tmp_path / "registry.json"))
    reset_registry_for_tests()
    start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
    route = {**pair(), "_systemDeleteRearmOpenPosition": 0.011}
    register_auto_created_pair(route, astro_pair={**route, "id": "pair-1"}, now=start)
    mark_auto_card_system_deleted(
        ("RVN", "FF", "gate", "okx"),
        "spread_below_ff_threshold",
        now=start + timedelta(minutes=3),
        cooldown_seconds=30,
    )
    monkeypatch.setattr(registry_module, "_utc_now", lambda: start + timedelta(minutes=3, seconds=20))
    assert astro_cleanup_status()["cooldownCount"] == 1

    allowed, suppressed, guarded = apply_delete_rearm_rules(
        [route], [], now=start + timedelta(minutes=3, seconds=20), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == []
    assert suppressed[0]["reason"] == "system_cleanup_cooldown"
    assert guarded == []

    below_rearm = {**route, "openPosition": "0.0109"}
    allowed, suppressed, guarded = apply_delete_rearm_rules(
        [below_rearm], [], now=start + timedelta(minutes=3, seconds=30), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == []
    assert suppressed[0]["reason"] == "system_cleanup_rearm_threshold"
    assert guarded == []
    monkeypatch.setattr(registry_module, "_utc_now", lambda: start + timedelta(minutes=3, seconds=30))
    assert astro_cleanup_status()["cooldownCount"] == 0

    at_rearm = {**route, "openPosition": "0.011"}
    allowed, suppressed, guarded = apply_delete_rearm_rules(
        [at_rearm], [], now=start + timedelta(minutes=3, seconds=31), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == [at_rearm]
    assert suppressed == []
    assert guarded == []

    # A failed creation retries at the same system threshold and must never be
    # mistaken for a manual deletion that activates the pullback/+20% guard.
    allowed, suppressed, guarded = apply_delete_rearm_rules(
        [at_rearm], [], now=start + timedelta(minutes=3, seconds=32), rearm_pct=20, confirmation_seconds=5
    )
    assert allowed == [at_rearm]
    assert suppressed == []
    assert guarded == []
    assert astro_delete_rearm_status()["activeGuardCount"] == 0


def test_final_rearm_checks_current_spread_without_advancing_state(monkeypatch, tmp_path):
    monkeypatch.setenv('ASTRO_AUTO_CARD_REGISTRY_FILE', str(tmp_path/'registry.json'))
    monkeypatch.setenv('ASTRO_SPREAD_SUBSCRIPTIONS_FILE', str(tmp_path/'subscriptions.json'))
    monkeypatch.setenv('ASTRO_AUTO_CARD_DELETE_REARM_PCT', '20')
    reset_registry_for_tests()
    start=datetime.now(timezone.utc)-timedelta(minutes=2)
    route=pair()
    register_auto_created_pair(route, now=start)
    apply_delete_rearm_rules([], [], now=start+timedelta(seconds=5), confirmation_seconds=5)
    apply_delete_rearm_rules([], [], now=start+timedelta(seconds=10), confirmation_seconds=5)
    saved=(tmp_path/'registry.json').read_bytes()
    check=registry_module.check_delete_rearm_before_submit
    assert check({**route,'openPosition':'.015'})[0]
    allowed, report=check({**route,'openPosition':'.013'})
    assert not allowed and report['rearmOpenPosition']==pytest.approx(.0144)
    assert (tmp_path/'registry.json').read_bytes()==saved
    assert check({**route,'name':'NEW'})[0]


def test_final_rearm_preserves_confirmed_pullback_path(monkeypatch, tmp_path):
    monkeypatch.setenv('ASTRO_AUTO_CARD_REGISTRY_FILE',str(tmp_path/'registry.json'))
    monkeypatch.setenv('ASTRO_SPREAD_SUBSCRIPTIONS_FILE',str(tmp_path/'subscriptions.json'))
    monkeypatch.setenv('ASTRO_AUTO_CARD_DELETE_PULLBACK_PCT_POINTS','0.5')
    start=datetime.now(timezone.utc)-timedelta(minutes=2);route=pair()
    register_auto_created_pair(route,now=start)
    apply_delete_rearm_rules([],[],now=start+timedelta(seconds=5),confirmation_seconds=5)
    apply_delete_rearm_rules([],[],now=start+timedelta(seconds=10),confirmation_seconds=5)
    obs={registry_module.pair_identity(route):{'state':'eligible','openSpreadPct':.6}}
    for seconds in (15,20):
        apply_delete_rearm_rules([],[],route_observations=obs,now=start+timedelta(seconds=seconds),confirmation_seconds=5)
    allowed,report=registry_module.check_delete_rearm_before_submit({**route,'openPosition':'.008'})
    assert allowed and report['reason']=='confirmed_pullback'


def test_final_system_cleanup_threshold_and_cooldown_are_preserved(monkeypatch,tmp_path):
    monkeypatch.setenv('ASTRO_AUTO_CARD_REGISTRY_FILE',str(tmp_path/'registry.json'))
    start=datetime.now(timezone.utc);route=pair()
    register_auto_created_pair(route,now=start)
    registry_module.mark_auto_card_system_deleted(registry_module.pair_identity(route),'spread_below_threshold',now=start,cooldown_seconds=30)
    monkeypatch.setattr(registry_module,'_utc_now',lambda:start+timedelta(seconds=5))
    assert not registry_module.check_delete_rearm_before_submit({**route,'openPosition':'.03','_systemDeleteRearmOpenPosition':.009})[0]
    monkeypatch.setattr(registry_module,'_utc_now',lambda:start+timedelta(seconds=31))
    assert not registry_module.check_delete_rearm_before_submit({**route,'openPosition':'.008','_systemDeleteRearmOpenPosition':.009})[0]
    assert registry_module.check_delete_rearm_before_submit({**route,'openPosition':'.01','_systemDeleteRearmOpenPosition':.009})[0]
