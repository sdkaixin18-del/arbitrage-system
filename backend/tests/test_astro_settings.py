import json
from pathlib import Path

import pytest

from app import astro_settings as settings, astro_sdk as sdk, astro_spread_scanner as scanner
from app import astro_news_policy as news


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"blockedCoins": ["BLOCK"], "fsBorrowAutoCardEnabled": True}))
    monkeypatch.setenv("ASTRO_SPREAD_SUBSCRIPTIONS_FILE", str(path))
    monkeypatch.setattr(settings, "_last_good", {})
    monkeypatch.setattr(news, "route_check", lambda *a: None)
    monkeypatch.setattr(sdk, "_log", lambda *a, **kw: None)
    return path


def pair(**kwargs):
    return {"name": "COIN", "type": "FF", "buyEx": "binance", "sellEx": "bitget", **kwargs}


@pytest.mark.parametrize("contents", ["{", "[]", '{"blockedCoins":null}', '{"blockedPairs":{}}',
                                       '{"blockedCoins":[{}]}', '{"fsBorrowAutoCardEnabled":"false"}',
                                       '{"ffMinOpenSpreadPct":"oops"}', '{"maxNotionalUsdt":NaN}'])
def test_bad_settings_never_allow_submission_or_partial_save(isolated, contents):
    assert scanner.spread_scan_blocked_coins() == ["BLOCK"]
    isolated.write_text(contents)
    assert scanner.spread_scan_blocked_coins() == ["BLOCK"]
    assert sdk._saved_auto_card_settings()["blockedCoins"] == ["BLOCK"]
    allowed, report = scanner.astro_spread_pair_submit_guard(pair())
    assert not allowed and report["reason"] == "settings_read_failed"
    assert settings.settings_health()["newCardsAllowed"] is False
    with pytest.raises(settings.SettingsReadError):
        scanner.update_astro_spread_subscriptions(["gateSpot", "gateFuture"])
    assert isolated.read_text() == contents


def test_missing_file_after_restart_blocks_and_recovers(isolated):
    isolated.unlink()
    assert not scanner.astro_spread_pair_submit_guard(pair())[0]
    assert not settings.settings_health()["newCardsAllowed"]
    isolated.write_text('{"blockedCoins":["COIN"]}')
    assert scanner.astro_spread_pair_submit_guard(pair())[1]["reason"] == "blocked_coin"
    isolated.write_text('{}')
    assert scanner.astro_spread_pair_submit_guard(pair())[0]


def test_permission_failure_blocks_without_exposing_path_or_rules(monkeypatch, isolated):
    settings.read_settings(strict=True)
    def denied(*a, **kw):
        raise PermissionError("sensitive path")
    monkeypatch.setattr(Path, "read_text", denied)
    assert not scanner.astro_spread_pair_submit_guard(pair())[0]
    assert "sensitive" not in settings.settings_health()["message"]
    assert settings.read_settings()["blockedCoins"] == ["BLOCK"]


def test_fs_guard_checks_spot_sell_block_news_and_pause(monkeypatch, isolated):
    fs = pair(type="FS")
    isolated.write_text('{"blockedPairs":[{"marketKey":"bitgetSpot","symbol":"COINUSDT"}],"fsBorrowAutoCardEnabled":true}')
    allowed, report = scanner.astro_spread_pair_submit_guard(fs)
    assert not allowed and report["matchedPairs"][0]["side"] == "sell"
    isolated.write_text('{"fsBorrowAutoCardEnabled":true}')
    monkeypatch.setattr(news, "route_check", lambda *a: {"reason": "delisted"})
    assert scanner.astro_spread_pair_submit_guard(fs) == (False, {"reason": "delisted"})
    isolated.write_text('{"fsBorrowAutoCardEnabled":false}')
    assert scanner.astro_spread_pair_submit_guard(fs)[1]["reason"] == "fs_borrow_auto_card_paused"


def test_sdk_checks_settings_even_without_caller_guard(isolated):
    isolated.write_text('{')
    assert sdk._sync_candidate_pair(object(), pair(), None, None, set()) is False


def test_unsupported_sell_dex_cannot_fall_into_cex_validation(monkeypatch):
    monkeypatch.setattr(scanner, "_record_revalidation_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(scanner, "_fetch_direct_route_once", lambda *a, **kw: pytest.fail("unsupported route queried"))
    result, report = scanner.revalidate_astro_spread_pair(pair(sellEx="okxdex"), None)
    assert result is None and report["reason"] == "unsupported_dex_sell_route"


def test_cached_display_cannot_mutate_rules_or_skip_fresh_submit_read(monkeypatch, isolated):
    settings.read_settings()["blockedCoins"].clear()
    assert settings.read_settings()["blockedCoins"] == ["BLOCK"]
    reads = []
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda p, **kw: reads.append(p) or original(p, **kw))
    settings.read_settings()
    assert reads == []
    settings.read_settings(strict=True)
    assert reads == [isolated]
    isolated.write_text('{"blockedCoins":["NEW"]}')
    assert settings.read_settings()["blockedCoins"] == ["NEW"]


def test_slow_reader_does_not_hold_global_settings_lock(monkeypatch, isolated, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    waiting, release = threading.Event(), threading.Event()
    other = tmp_path / 'other.json'
    other.write_text('{}')
    original = Path.read_text
    def slow(path, **kw):
        if path == isolated:
            waiting.set()
            assert release.wait(3)
        return original(path, **kw)
    monkeypatch.setattr(Path, 'read_text', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(settings.read_settings, isolated, strict=True)
        try:
            assert waiting.wait(1)
            assert pool.submit(settings.read_settings, other, strict=True).result(timeout=1) == {}
        finally:
            release.set()
        assert first.result()["blockedCoins"] == ["BLOCK"]
