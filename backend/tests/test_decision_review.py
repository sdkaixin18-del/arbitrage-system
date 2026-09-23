from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.decision_review import DecisionCreate, _overview, create_decision
from app.models import InformationScreeningBatch, InformationScreeningItem, StockDailyBar


def _seed_item(db: Session, batch: InformationScreeningBatch, key: str) -> InformationScreeningItem:
    row = InformationScreeningItem(
        batch_id=batch.id,
        item_key=key,
        title=f"{key} 首次出现订单变化",
        summary="公司订单变化待跟踪",
        source_type="zsxq",
        source_name="调研信息",
        bucket="verified",
        marginal_change="订单首次得到交叉确认",
        verification_status="verified",
        evidence_summary="公告和产业链互证",
        related_stocks_json='["测试股份 688800"]',
        price_status="untraded",
        price_summary="股价尚未交易新增订单",
    )
    db.add(row)
    db.flush()
    return row


def test_decision_freezes_snapshot_and_uses_next_tradable_open(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'decision-review.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        batch = InformationScreeningBatch(batch_key="decision-test", title="决策测试")
        db.add(batch)
        db.flush()
        item = _seed_item(db, batch, "order-change")
        for index in range(1, 61):
            db.add(StockDailyBar(
                code="688800",
                name="测试股份",
                exchange="SH",
                full_code="SH688800",
                trade_date=date(2026, 7, 1) + timedelta(days=index),
                open=100.0,
                high=101.0 + index,
                low=99.0 - index * 0.1,
                close=100.0 + index,
                source="test",
            ))
        db.commit()

        row = create_decision(
            DecisionCreate(
                information_item_id=item.id,
                decision_code="A",
                primary_stock_code="688800",
                primary_stock_name="测试股份",
                thesis="订单变化成立",
                pricing_verdict="尚未反映订单增量",
                planned_horizon=20,
            ),
            db,
            clock=lambda: datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        )

        assert row.entry_date == date(2026, 7, 2)
        assert row.entry_price == 100.0
        assert row.execution_status == "matured"
        result = _overview(db)
        five_day = result["items"][0]["performance"]["horizons"]["5"]
        assert five_day["return_pct"] == pytest.approx(4.8)
        assert five_day["mfe_pct"] == pytest.approx(5.8)
        assert five_day["mae_pct"] == pytest.approx(-1.7)
        assert result["summary"]["horizons"]["20"]["sample_count"] == 1
        assert "下一交易日开盘模拟成交" in row.snapshot_json

        with pytest.raises(HTTPException) as duplicate:
            create_decision(
                DecisionCreate(
                    information_item_id=item.id,
                    decision_code="B",
                    primary_stock_code="688800",
                    primary_stock_name="测试股份",
                    thesis="重复判断",
                    pricing_verdict="等待价格",
                ),
                db,
            )
        assert duplicate.value.status_code == 409


def test_non_a_decisions_are_shadow_tracked_and_d_can_be_direction_only(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'decision-shadow.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        batch = InformationScreeningBatch(batch_key="shadow-test", title="影子样本测试")
        db.add(batch)
        db.flush()
        b_item = _seed_item(db, batch, "wait-price")
        d_item = _seed_item(db, batch, "filter-direction")
        db.commit()

        b_row = create_decision(
            DecisionCreate(
                information_item_id=b_item.id,
                decision_code="B",
                primary_stock_code="688800",
                primary_stock_name="测试股份",
                thesis="变化成立但赔率不足",
                pricing_verdict="股价已提前交易",
            ),
            db,
            clock=lambda: datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
        d_row = create_decision(
            DecisionCreate(
                information_item_id=d_item.id,
                decision_code="D",
                thesis="产业映射过远",
                pricing_verdict="不构成可交易表达",
            ),
            db,
            clock=lambda: datetime(2026, 7, 15, tzinfo=timezone.utc),
        )

        assert b_row.execution_status == "shadow_waiting"
        assert d_row.execution_status == "no_stock"
        result = _overview(db)
        assert result["summary"]["decision_counts"] == {"A": 0, "B": 1, "C": 0, "D": 1}
        assert result["summary"]["pipeline"]["decision_total"] == 2
