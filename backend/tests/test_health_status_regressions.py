from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import crypto, funding_cloud
from app.database import Base
from app.models import CryptoFundingFormationWatchItem


def test_paused_fs_empty_result_has_no_pending_scans(monkeypatch):
    monkeypatch.setattr(crypto, "fs_signal_cached_response", lambda _: (None, True))
    monkeypatch.setattr(crypto, "astro_auto_card_status", lambda: {})
    monkeypatch.setattr(crypto, "astro_spread_scanner_status", lambda: {})
    monkeypatch.setattr(crypto, "start_fs_signal_background_scan", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not scan")))
    result = crypto.crypto_fs_signals_overview(None, limit=5, allow_scan=False)
    assert result["scanning"] is False
    assert all(c["status"] == "paused" for c in result["exchangeChecks"].values())
    assert all("后台扫描中" not in c["message"] for c in result["exchangeChecks"].values())


def test_paused_cached_fs_checks_and_resume_are_consistent(monkeypatch):
    monkeypatch.setattr(crypto, "astro_auto_card_status", lambda: {})
    monkeypatch.setattr(crypto, "astro_spread_scanner_status", lambda: {})
    monkeypatch.setattr(crypto, "normalize_fs_signals_payload", lambda p: p)
    monkeypatch.setattr(crypto, "_fs_signal_scan_state", {5: {"running": False, "paused": True}})
    payload = {"exchangeChecks": {"bg": {"status": "pending"}, "bn": {"status": "error", "message": "old error"}}}
    paused = crypto.attach_fs_signal_scan_state(5, payload)
    assert paused["exchangeChecks"]["bg"]["status"] == "paused"
    assert paused["exchangeChecks"]["bn"]["status"] == "error"
    crypto._fs_signal_scan_state[5] = {"running": True, "startedAt": datetime.now(timezone.utc)}
    resumed = crypto.attach_fs_signal_scan_state(5, paused)
    assert resumed["scanning"] is True
    assert resumed["exchangeChecks"]["bg"]["status"] == "pending"


def test_cloud_health_counts_only_enabled_watches(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(funding_cloud, "SessionLocal", lambda: Session(engine))
    with Session(engine) as db:
        db.add_all([
            CryptoFundingFormationWatchItem(exchange="bn", symbol="HIVE", enabled=True),
            CryptoFundingFormationWatchItem(exchange="gt", symbol="HIVE", enabled=False),
        ])
        db.commit()
    payload = funding_cloud.health()
    assert payload["watchCount"] == 1
    assert payload["totalWatchCount"] == 2
    assert payload["disabledWatchCount"] == 1
    assert payload["watchCount"] == funding_cloud.watch()["itemCount"]
