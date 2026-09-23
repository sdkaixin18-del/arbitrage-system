import json
from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.factor_review import (
    FactorReviewBatchCreate,
    batch_to_out,
    confirm_factor_review_item,
    create_factor_review_batch,
    list_item_versions,
    refresh_factor_review_research,
    reopen_factor_review_item,
    save_factor_review_draft,
    submit_factor_review_initial,
)
from app.models import (
    FactorQuoteSnapshot,
    FactorReviewBatch,
    FactorStockTag,
    FactorTag,
    MarketStyleThsBar,
    MarketStyleThsMember,
    StockDailyBar,
    XueqiuPost,
    XueqiuTarget,
)


def test_factor_review_two_stage_confirmation_and_versions() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    trade_date = date(2026, 7, 10)

    with Session(engine) as db:
        rows = [
            ("SZ000001", "甲公司", 10.0, 100.0),
            ("SZ000002", "乙公司", 8.0, 400.0),
            ("SZ000003", "丙公司", 2.0, 300.0),
            ("SZ000004", "丁公司", -1.0, 200.0),
        ]
        for full_code, name, change_pct, amount in rows:
            code = full_code[-6:]
            db.add(
                FactorQuoteSnapshot(
                    code=code,
                    name=name,
                    exchange="SZ",
                    full_code=full_code,
                    latest_price=10,
                    change_pct=change_pct,
                    returns_json=json.dumps({"1": change_pct}),
                    trade_date=trade_date,
                )
            )
            db.add(
                StockDailyBar(
                    code=code,
                    name=name,
                    exchange="SZ",
                    full_code=full_code,
                    trade_date=trade_date,
                    open=10,
                    high=11,
                    low=9,
                    close=10.5,
                    change_pct=change_pct,
                    amount=amount,
                    source="test",
                )
            )
        target_user = XueqiuTarget(nickname="测试用户", watchlist_url="https://xueqiu.com/test")
        db.add(target_user)
        db.flush()
        db.add(XueqiuPost(target_id=target_user.id, external_id="post-1", author_name="测试用户", content="甲公司正在验证AI算力新产品，仍需等待订单。"))
        db.add(MarketStyleThsMember(group_type="concept", group_code="C1", group_name="AI算力", code="000001", name="甲公司", exchange="SZ", full_code="SZ000001", source="test"))
        db.add(MarketStyleThsMember(group_type="industry", group_code="I1", group_name="软件开发", code="000001", name="甲公司", exchange="SZ", full_code="SZ000001", source="test"))
        db.add(MarketStyleThsBar(group_type="concept", group_code="C1", group_name="AI算力", trade_date=trade_date, open=100, high=105, low=99, close=104, change_pct=3.2, source="test"))
        db.add(MarketStyleThsBar(group_type="industry", group_code="I1", group_name="软件开发", trade_date=trade_date, open=100, high=102, low=99, close=101, change_pct=1.0, source="test"))
        db.commit()

        batch, created = create_factor_review_batch(db, FactorReviewBatchCreate(trade_date=trade_date, gain_limit=2, amount_limit=2))
        assert created is True
        assert batch["stock_count"] == 3
        overlap = next(item for item in batch["items"] if item["full_code"] == "SZ000002")
        assert overlap["gain_rank"] == 2
        assert overlap["amount_rank"] == 1

        target = next(item for item in batch["items"] if item["full_code"] == "SZ000001")
        item_id = target["id"]
        with pytest.raises(ValueError, match="请先刷新"):
            submit_factor_review_initial(db, item_id)
        researched = refresh_factor_review_research(db, item_id, include_live_zsxq=False)
        assert researched["research_status"] == "ready"
        assert "AI算力" in researched["suggested_tags"]
        assert any(row["category"] == "xueqiu" for row in researched["evidence"])
        assert any(row["key"] == "ths" and row["count"] == 2 for row in researched["source_statuses"])
        saved = save_factor_review_draft(db, item_id, ["强势股", "AI"])
        assert saved["stage"] == "initial_pending"
        first = submit_factor_review_initial(db, item_id)
        assert first["stage"] == "second_pending"
        assert first["initial_tags"] == ["强势股", "AI"]

        revised = save_factor_review_draft(db, item_id, ["AI", "光模块"])
        assert revised["draft_tags"] == ["AI", "光模块"]
        confirmed = confirm_factor_review_item(db, item_id)
        assert confirmed["stage"] == "confirmed"
        assert confirmed["effective_tags"] == ["AI", "光模块"]
        assert db.scalar(select(func.count(FactorTag.id))) == 2
        assert db.scalar(select(func.count(FactorStockTag.id))) == 2

        reopened = reopen_factor_review_item(db, item_id)
        assert reopened["stage"] == "second_pending"
        save_factor_review_draft(db, item_id, ["AI"])
        reconfirmed = confirm_factor_review_item(db, item_id)
        assert reconfirmed["effective_tags"] == ["AI"]
        assert db.scalar(select(func.count(FactorStockTag.id))) == 1
        assert len(list_item_versions(db, item_id)) == 8

        batch_row = db.scalar(select(FactorReviewBatch))
        summary = batch_to_out(db, batch_row)
        assert summary["confirmed_count"] == 1
        assert summary["initial_pending_count"] == 2
