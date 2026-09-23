from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.database import get_data_dir, get_database_path
from app.models import (
    CryptoMarketQuoteSnapshot,
    CryptoFsRuntimeLog,
    CryptoPushLog,
    CryptoSpreadSnapshot,
    ExchangeAnnouncementPushLog,
    FactorRefreshRun,
    FactorTagCandidateRun,
    WatchlistAnnouncementCrawlLog,
    WatchlistAnnouncementItem,
    WatchlistAnnouncementPushLog,
)

LAST_CLEANUP_FILE = "cache-cleanup-last.json"


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


def cache_cleanup_enabled() -> bool:
    return os.environ.get("CACHE_CLEANUP_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def retention_days() -> dict[str, int]:
    return {
        "watchlist_announcement_items": env_int("WATCHLIST_ANN_ITEM_RETENTION_DAYS", 60, 1),
        "watchlist_announcement_crawl_logs": env_int("WATCHLIST_ANN_CRAWL_LOG_RETENTION_DAYS", 30, 1),
        "watchlist_announcement_push_logs": env_int("WATCHLIST_ANN_PUSH_LOG_RETENTION_DAYS", 90, 1),
        "exchange_announcement_push_logs": env_int("EXCHANGE_ANN_PUSH_LOG_RETENTION_DAYS", 90, 1),
        "crypto_spread_snapshots": env_int("CRYPTO_SNAPSHOT_RETENTION_DAYS", 7, 1),
        "crypto_market_quote_snapshots": env_int("CRYPTO_MARKET_QUOTE_RETENTION_DAYS", 3, 1),
        "crypto_fs_runtime_logs": env_int("FS_RUNTIME_LOG_RETENTION_DAYS", 7, 1),
        "crypto_push_logs": env_int("CRYPTO_PUSH_LOG_RETENTION_DAYS", 90, 1),
        "factor_refresh_runs": env_int("FACTOR_REFRESH_LOG_RETENTION_DAYS", 30, 1),
        "factor_tag_candidate_runs": env_int("FACTOR_TAG_CANDIDATE_LOG_RETENTION_DAYS", 30, 1),
    }


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            stat = path.stat()
            return int(getattr(stat, "st_blocks", 0) or 0) * 512 or stat.st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for filename in files:
            file_path = Path(root) / filename
            try:
                stat = file_path.stat()
                total += int(getattr(stat, "st_blocks", 0) or 0) * 512 or stat.st_size
            except OSError:
                continue
    return total


def table_counts(db: Session) -> dict[str, int]:
    models = {
        "watchlist_announcement_items": WatchlistAnnouncementItem,
        "watchlist_announcement_crawl_logs": WatchlistAnnouncementCrawlLog,
        "watchlist_announcement_push_logs": WatchlistAnnouncementPushLog,
        "exchange_announcement_push_logs": ExchangeAnnouncementPushLog,
        "crypto_spread_snapshots": CryptoSpreadSnapshot,
        "crypto_market_quote_snapshots": CryptoMarketQuoteSnapshot,
        "crypto_fs_runtime_logs": CryptoFsRuntimeLog,
        "crypto_push_logs": CryptoPushLog,
        "factor_refresh_runs": FactorRefreshRun,
        "factor_tag_candidate_runs": FactorTagCandidateRun,
    }
    return {
        name: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for name, model in models.items()
    }


def last_cleanup_path() -> Path:
    return get_data_dir() / LAST_CLEANUP_FILE


def load_last_cleanup_result() -> dict[str, Any] | None:
    path = last_cleanup_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_last_cleanup_result(result: dict[str, Any]) -> None:
    path = last_cleanup_path()
    try:
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def cache_status(db: Session) -> dict[str, Any]:
    db_path = get_database_path()
    data_dir = get_data_dir()
    return {
        "status": "ok",
        "enabled": cache_cleanup_enabled(),
        "data_dir": str(data_dir),
        "database_path": str(db_path),
        "database_size_bytes": path_size(db_path),
        "site_data_size_bytes": path_size(data_dir),
        "retention_days": retention_days(),
        "table_counts": table_counts(db),
        "last_cleanup": load_last_cleanup_result(),
    }


def delete_older_than(db: Session, model: Any, column: Any, days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = db.execute(delete(model).where(column < cutoff))
    return max(0, int(result.rowcount or 0))


def cleanup_database_rows(db: Session) -> dict[str, int]:
    days = retention_days()
    deleted: dict[str, int] = {}
    deleted["watchlist_announcement_items"] = delete_older_than(
        db,
        WatchlistAnnouncementItem,
        WatchlistAnnouncementItem.crawled_at,
        days["watchlist_announcement_items"],
    )
    deleted["watchlist_announcement_crawl_logs"] = delete_older_than(
        db,
        WatchlistAnnouncementCrawlLog,
        WatchlistAnnouncementCrawlLog.created_at,
        days["watchlist_announcement_crawl_logs"],
    )
    deleted["watchlist_announcement_push_logs"] = delete_older_than(
        db,
        WatchlistAnnouncementPushLog,
        WatchlistAnnouncementPushLog.created_at,
        days["watchlist_announcement_push_logs"],
    )
    deleted["exchange_announcement_push_logs"] = delete_older_than(
        db,
        ExchangeAnnouncementPushLog,
        ExchangeAnnouncementPushLog.created_at,
        days["exchange_announcement_push_logs"],
    )
    deleted["crypto_spread_snapshots"] = delete_older_than(
        db,
        CryptoSpreadSnapshot,
        CryptoSpreadSnapshot.created_at,
        days["crypto_spread_snapshots"],
    )
    deleted["crypto_market_quote_snapshots"] = delete_older_than(
        db,
        CryptoMarketQuoteSnapshot,
        CryptoMarketQuoteSnapshot.created_at,
        days["crypto_market_quote_snapshots"],
    )
    deleted["crypto_fs_runtime_logs"] = delete_older_than(
        db,
        CryptoFsRuntimeLog,
        CryptoFsRuntimeLog.created_at,
        days["crypto_fs_runtime_logs"],
    )
    deleted["crypto_push_logs"] = delete_older_than(
        db,
        CryptoPushLog,
        CryptoPushLog.created_at,
        days["crypto_push_logs"],
    )
    deleted["factor_refresh_runs"] = delete_older_than(
        db,
        FactorRefreshRun,
        FactorRefreshRun.started_at,
        days["factor_refresh_runs"],
    )
    deleted["factor_tag_candidate_runs"] = delete_older_than(
        db,
        FactorTagCandidateRun,
        FactorTagCandidateRun.started_at,
        days["factor_tag_candidate_runs"],
    )
    db.commit()
    return deleted


def run_cache_cleanup(db: Session, reason: str = "manual") -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    if not cache_cleanup_enabled():
        result = {
            "status": "manual_only",
            "reason": reason,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "message": "缓存清理已关闭。",
            "deleted_rows": {},
            "browser_cache": {"freed_bytes": 0, "removed_dirs": []},
        }
        save_last_cleanup_result(result)
        return result

    before_status = cache_status(db)
    try:
        deleted_rows = cleanup_database_rows(db)
        browser_cache = {"freed_bytes": 0, "removed_dirs": []}
        after_status = cache_status(db)
        result = {
            "status": "ok",
            "reason": reason,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "message": "缓存清理完成。",
            "deleted_rows": deleted_rows,
            "browser_cache": browser_cache,
            "before": {
                "site_data_size_bytes": before_status["site_data_size_bytes"],
                "database_size_bytes": before_status["database_size_bytes"],
            },
            "after": {
                "site_data_size_bytes": after_status["site_data_size_bytes"],
                "database_size_bytes": after_status["database_size_bytes"],
            },
        }
    except Exception as exc:
        db.rollback()
        result = {
            "status": "error",
            "reason": reason,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "message": str(exc),
            "deleted_rows": {},
            "browser_cache": {"freed_bytes": 0, "removed_dirs": []},
        }
    save_last_cleanup_result(result)
    return result
