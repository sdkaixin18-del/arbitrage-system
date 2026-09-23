from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.env import load_project_env

load_project_env()

DEFAULT_EXTERNAL_VOLUME = Path.home() / "Documents"
DEFAULT_DATA_ROOT_NAME = "套利系统"
SCHEMA_VERSION = 21


def _external_drive_required() -> bool:
    value = os.environ.get("STOCK_REVIEW_REQUIRE_EXTERNAL_DRIVE", "0").lower()
    return value not in {"0", "false", "no"}


def require_external_volume() -> Path:
    volume = Path(os.environ.get("STOCK_REVIEW_EXTERNAL_VOLUME", str(DEFAULT_EXTERNAL_VOLUME))).expanduser()
    if _external_drive_required() and not volume.is_dir():
        raise RuntimeError(f"请插入新加卷外接硬盘后再启动后台：{volume}")
    return volume


def get_data_root() -> Path:
    volume = require_external_volume()
    configured = os.environ.get("STOCK_REVIEW_DATA_ROOT")
    if configured:
        return Path(configured).expanduser()
    return volume / DEFAULT_DATA_ROOT_NAME


def ensure_external_path(path: Path) -> None:
    if not _external_drive_required():
        return
    volume = require_external_volume().resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(volume)
    except ValueError as exc:
        raise RuntimeError(f"数据目录必须位于外接硬盘 {volume} 内，当前是 {resolved}") from exc


def get_data_dir() -> Path:
    configured = os.environ.get("STOCK_REVIEW_DATA_DIR")
    data_dir = Path(configured).expanduser() if configured else get_data_root() / "site-data"
    ensure_external_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_database_path() -> Path:
    return get_data_dir() / "app.db"


class Base(DeclarativeBase):
    pass


engine = create_engine(
    f"sqlite:///{get_database_path()}",
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(engine, "connect")
def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> bool:
    from app import models  # noqa: F401

    with engine.connect() as connection:
        current_version = int(connection.exec_driver_sql("PRAGMA user_version").scalar() or 0)
    Base.metadata.create_all(bind=engine)
    ensure_sqlite_columns()
    if current_version < SCHEMA_VERSION:
        migrate_industry_trend_v1()
    ensure_sqlite_indexes()
    if current_version < SCHEMA_VERSION:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return current_version < SCHEMA_VERSION


def ensure_sqlite_columns() -> None:
    migrations = {
        "xq_watchlist_snapshots": {"price": "FLOAT"},
        "xq_watchlist_events": {"price": "FLOAT"},
        "xq_recommendations": {
            "source_post_id": "INTEGER",
            "source_type": "VARCHAR(24) DEFAULT 'auto'",
            "end_price": "FLOAT",
            "source_url": "TEXT",
            "source_excerpt": "TEXT",
            "closed_at": "DATETIME",
            "updated_at": "DATETIME",
        },
        "exchange_announcement_push_logs": {
            "event_at": "DATETIME",
            "asset_type": "VARCHAR(32) DEFAULT 'unknown'",
            "asset_label": "VARCHAR(40) DEFAULT '未分类'",
            "visible": "BOOLEAN DEFAULT 1",
        },
        "crypto_watch_items": {
            "left_market_type": "VARCHAR(16) DEFAULT 'futures'",
            "right_market_type": "VARCHAR(16) DEFAULT 'futures'",
        },
        "crypto_symbol_mappings": {
            "price_ratio": "FLOAT DEFAULT 1.0",
        },
        "crypto_funding_cap_watch_items": {
            "exchanges_json": "TEXT NOT NULL DEFAULT '[\"bn\",\"by\",\"gt\",\"okx\",\"bg\",\"as\"]'",
        },
        "crypto_funding_cap_events": {
            "previous_funding_interval_hours": "FLOAT",
        },
        "crypto_spread_snapshots": {
            "left_market_type": "VARCHAR(16) DEFAULT 'futures'",
            "right_market_type": "VARCHAR(16) DEFAULT 'futures'",
            "bid_spread_pct": "FLOAT",
            "ask_spread_pct": "FLOAT",
        },
        "crypto_funding_formation_prediction_logs": {
            "prediction_model_version": "VARCHAR(80)",
            "calculation_details_json": "TEXT",
            "shadow_formula_version": "VARCHAR(80)",
            "shadow_predicted_rate": "FLOAT",
            "shadow_absolute_error": "FLOAT",
            "shadow_success": "BOOLEAN",
        },
        "factor_stock_reviews": {
            "reviewed_at": "DATETIME",
            "reviewed_reasons_json": "TEXT",
            "reviewed_reason_signature": "VARCHAR(512)",
            "reviewed_tag_signature": "VARCHAR(512)",
        },
        "sector_index_bars": {"amount": "FLOAT"},
        "factor_review_items": {
            "market_tags_json": "TEXT NOT NULL DEFAULT '[]'",
            "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            "source_status_json": "TEXT NOT NULL DEFAULT '[]'",
            "research_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
            "researched_at": "DATETIME",
        },
        "future_event_feedback": {
            "reason_category": "VARCHAR(48)",
        },
        "information_screening_items": {
            "official_check_status": "VARCHAR(32) NOT NULL DEFAULT 'not_required'",
            "official_check_message": "TEXT NOT NULL DEFAULT ''",
            "official_source_url": "TEXT",
            "official_stock_codes_json": "TEXT NOT NULL DEFAULT '[]'",
            "official_checked_at": "DATETIME",
        },
        "industry_chains": {
            "investment_logic": "TEXT",
            "change_summary": "TEXT",
            "why_now": "TEXT",
            "drivers_json": "TEXT NOT NULL DEFAULT '[]'",
            "expected_duration": "VARCHAR(120)",
            "attention_level": "VARCHAR(32) NOT NULL DEFAULT '观察'",
            "direction_verdict": "VARCHAR(16) NOT NULL DEFAULT '观察'",
            "stock_verdict": "VARCHAR(16) NOT NULL DEFAULT '观察'",
            "timing_verdict": "VARCHAR(16) NOT NULL DEFAULT '观察'",
            "overall_verdict": "VARCHAR(16) NOT NULL DEFAULT '观察'",
            "pricing_status": "VARCHAR(24) NOT NULL DEFAULT '部分定价'",
            "priced_in": "TEXT",
            "not_priced_in": "TEXT",
            "next_signal": "TEXT",
            "invalidation": "TEXT",
            "primary_company_id": "INTEGER",
            "phase_entered_at": "DATE",
            "last_change_at": "DATETIME",
            "revision": "INTEGER NOT NULL DEFAULT 0",
        },
        "industry_chain_companies": {
            "market": "VARCHAR(32) NOT NULL DEFAULT 'A股'",
            "external_url": "TEXT",
            "company_standing": "TEXT",
            "benefit_directness": "VARCHAR(24)",
            "profit_path": "TEXT",
            "verification_status": "VARCHAR(24) NOT NULL DEFAULT '未验证'",
            "pricing_status": "VARCHAR(24) NOT NULL DEFAULT '部分定价'",
            "is_global_leader": "BOOLEAN NOT NULL DEFAULT 0",
            "is_domestic_alternative": "BOOLEAN NOT NULL DEFAULT 0",
            "is_primary": "BOOLEAN NOT NULL DEFAULT 0",
            "primary_reason": "TEXT",
            "node_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "market_implied_expectation": "TEXT",
            "evidence_based_expectation": "TEXT",
            "expectation_gap_status": "VARCHAR(24) NOT NULL DEFAULT '无法判断'",
            "expectation_gap_reason": "TEXT",
            "expectation_trigger": "TEXT",
            "expectation_invalidation": "TEXT",
            "expectation_as_of": "DATE",
            "expectation_anchor_market_cap": "FLOAT",
            "expectation_evidence_growth_pct": "FLOAT",
            "expectation_evidence_acceleration_pct": "FLOAT",
        },
        "industry_chain_tasks": {
            "node_id": "INTEGER",
            "criteria": "TEXT",
            "current_result": "TEXT",
            "source_name": "VARCHAR(160)",
            "source_url": "TEXT",
            "sort_order": "INTEGER NOT NULL DEFAULT 100",
        },
        "industry_trend_nodes": {
            "plain_explanation": "TEXT",
            "value_flow": "TEXT",
            "watch_signal": "TEXT",
            "maturity_status": "VARCHAR(24)",
        },
        "industry_chain_evidence": {
            "source_tier": "VARCHAR(24) NOT NULL DEFAULT '官方硬证据'",
            "verification_status": "VARCHAR(24) NOT NULL DEFAULT '单一来源'",
            "evidence_state": "VARCHAR(24) NOT NULL DEFAULT '有效'",
            "valid_until": "DATE",
            "conflict_note": "TEXT",
        },
        "industry_trend_generation_jobs": {
            "input_token_estimate": "INTEGER NOT NULL DEFAULT 0",
            "output_token_estimate": "INTEGER NOT NULL DEFAULT 0",
            "change_count": "INTEGER NOT NULL DEFAULT 0",
        },
        "industry_trend_updates": {
            "causal_stage": "VARCHAR(24)",
            "evidence_type": "VARCHAR(24)",
            "signal_status": "VARCHAR(24)",
            "buyer_group": "VARCHAR(160)",
            "market_response": "TEXT",
            "sell_pressure": "VARCHAR(16)",
            "counter_evidence": "TEXT",
        },
    }
    with engine.begin() as connection:
        for table, columns in migrations.items():
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            for column, definition in columns.items():
                if column not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
        connection.execute(
            text(
                """
                UPDATE crypto_funding_formation_prediction_logs
                SET prediction_model_version = CASE
                    WHEN prediction_method = 'shrunk_latest_premium_carry_forward_v2'
                        THEN 'funding_formation_v2_shrink_85_15'
                    WHEN prediction_method = 'latest_premium_carry_forward'
                        THEN 'funding_formation_v1_latest_tick'
                    ELSE prediction_method
                END
                WHERE prediction_model_version IS NULL
                  AND prediction_method IS NOT NULL
                """
            )
        )


def migrate_industry_trend_v1() -> None:
    phase_map = {
        "观察": "观察期",
        "启动期": "萌芽期",
        "确认期": "验证期",
        "扩散期": "增长期",
        "退潮期": "退潮期",
    }
    with engine.begin() as connection:
        # 只为旧数据建立第一版候选标识；后续以人工编辑或Codex草稿为准。
        connection.execute(
            text(
                """
                UPDATE industry_chain_companies
                SET is_global_leader = 1
                WHERE verification_status = '已确认'
                  AND (
                    lower(coalesce(company_standing, '') || ' ' || coalesce(core_logic, '')) LIKE '%全球%龙头%'
                    OR lower(coalesce(company_standing, '') || ' ' || coalesce(core_logic, '')) LIKE '%全球%领先%'
                    OR lower(coalesce(company_standing, '') || ' ' || coalesce(core_logic, '')) LIKE '%全球%核心%'
                    OR lower(coalesce(company_standing, '') || ' ' || coalesce(core_logic, '')) LIKE '%全球%平台%'
                    OR coalesce(company_standing, '') LIKE '%领导者%'
                    OR coalesce(company_standing, '') LIKE '%核心供应商%'
                  )
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE industry_chain_companies
                SET is_domestic_alternative = 1
                WHERE market = 'A股'
                  AND (
                    coalesce(company_standing, '') || ' ' || coalesce(core_logic, '') LIKE '%国产%'
                    OR coalesce(company_standing, '') || ' ' || coalesce(core_logic, '') LIKE '%替代%'
                    OR coalesce(company_standing, '') || ' ' || coalesce(core_logic, '') LIKE '%追赶%'
                  )
                """
            )
        )
        for old_phase, new_phase in phase_map.items():
            connection.execute(
                text("UPDATE industry_chains SET phase = :new_phase WHERE phase = :old_phase"),
                {"new_phase": new_phase, "old_phase": old_phase},
            )
        connection.execute(
            text(
                """
                UPDATE industry_chains
                SET phase_entered_at = date(updated_at)
                WHERE phase_entered_at IS NULL
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE industry_chains
                SET attention_level = '暂停', status = 'paused'
                WHERE name LIKE '示例：%'
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE industry_chain_companies
                SET tracking_status = '观察'
                WHERE tracking_status NOT IN ('核心受益', '重点跟踪', '观察', '淘汰')
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE industry_chain_companies
                SET market = 'A股'
                WHERE market IS NULL OR trim(market) = ''
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE industry_trend_nodes
                SET node_type = '全球公司'
                WHERE node_type = 'A股公司'
                """
            )
        )
        node_type_map = {
            "需求端": "需求驱动",
            "核心环节": "光互联产品",
            "价值量集中环节": "核心器件",
            "全球公司": "制造与配套",
        }
        for old_type, new_type in node_type_map.items():
            connection.execute(
                text("UPDATE industry_trend_nodes SET node_type = :new_type WHERE node_type = :old_type"),
                {"new_type": new_type, "old_type": old_type},
            )


def ensure_sqlite_indexes() -> None:
    statements = [
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_market_quote_pair_history
        ON crypto_market_quote_snapshots (symbol, exchange, market_type, source, batch_time)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_market_quote_negative_funding_scan
        ON crypto_market_quote_snapshots (market_type, status, exchange, batch_time, funding_rate)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_fs_signal_key_created
        ON crypto_fs_signal_logs (signal_key, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_fs_runtime_scan_created
        ON crypto_fs_runtime_logs (scan_id, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_fs_runtime_level_created
        ON crypto_fs_runtime_logs (level, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_fs_runtime_symbol_created
        ON crypto_fs_runtime_logs (symbol, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_compound_open_signal_key_created
        ON crypto_compound_open_signal_logs (signal_key, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_compound_open_signal_scope_created
        ON crypto_compound_open_signal_logs (strategy, symbol, left_exchange, right_exchange, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_borrow_watch_symbol_exchange_created
        ON crypto_borrow_watch_logs (symbol, exchange, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_coin_status_symbol_exchange_created
        ON crypto_coin_status_logs (symbol, exchange, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_index_component_change_symbol_created
        ON crypto_index_component_change_logs (symbol, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_index_component_change_scope
        ON crypto_index_component_change_logs (symbol, exchange, component, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_monitor_event_symbol_created
        ON crypto_monitor_events (symbol, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_monitor_event_scope_created
        ON crypto_monitor_events (symbol, exchange, event_type, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_crypto_monitor_event_ack_created
        ON crypto_monitor_events (acknowledged_at, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_xq_watchlist_snapshot_target_time_id
        ON xq_watchlist_snapshots (target_id, snapshot_time, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_xq_watchlist_snapshot_target_code_time
        ON xq_watchlist_snapshots (target_id, full_code, snapshot_time, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_xq_watchlist_snapshot_code_time_id
        ON xq_watchlist_snapshots (full_code, snapshot_time, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_xq_crawl_log_scope_target_status_created
        ON xq_crawl_logs (scope, target_id, status, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_xq_recommendation_target_created_status
        ON xq_recommendations (target_id, created_at, status)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_factor_review_items_research_status
        ON factor_review_items (research_status, stage, batch_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_industry_chains_trend_pool
        ON industry_chains (status, attention_level, last_change_at, strength, sort_order)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_industry_trend_updates_chain_date
        ON industry_trend_updates (chain_id, update_date, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_industry_trend_catalysts_chain_order
        ON industry_trend_catalysts (chain_id, sort_order, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_industry_chain_tasks_chain_order
        ON industry_chain_tasks (chain_id, sort_order, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_industry_trend_generation_job_status
        ON industry_trend_generation_jobs (chain_id, status, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_industry_trend_research_setting_last_run
        ON industry_trend_research_settings (chain_id, last_researched_at)
        """,
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
