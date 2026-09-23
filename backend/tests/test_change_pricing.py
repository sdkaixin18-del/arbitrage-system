from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.change_pricing import ChangePricingInput, analysis_to_out, upsert_change_pricing
from app.models import ChangePricingAnalysis, ChangePricingFeedback


def test_change_pricing_upsert_and_score() -> None:
    engine = create_engine("sqlite:///:memory:")
    ChangePricingAnalysis.__table__.create(engine)

    with Session(engine) as db:
        payload = ChangePricingInput(
            analysis_key="test-change",
            subject_name="测试股份",
            subject_code="SZ000001",
            change_title="首次获得大客户订单",
            change_date=date(2026, 7, 10),
            source_tier="A",
            fundamental_score=4.5,
            freshness_score=4.5,
            chain_score=4.0,
            evidence_score=4.2,
            chart_score=2.0,
            excess_return=15,
            fair_value_uplift=40,
            earnings_revision=20,
            valuation_percentile=45,
            crowding=35,
            chain_diffusion=40,
            days_elapsed=5,
            reflected_items=["行业方向"],
            unreflected_items=["盈利预测上调"],
        )
        row, created = upsert_change_pricing(db, payload)
        db.commit()
        db.refresh(row)
        output = analysis_to_out(row)

        assert created is True
        assert output.change_score > 75
        assert output.pricing_stage == "early"
        assert output.reflected_items == ["行业方向"]

        updated, created_again = upsert_change_pricing(db, payload.model_copy(update={"chart_score": 5.0}))
        db.commit()
        assert created_again is False
        assert updated.id == row.id


def test_change_pricing_feedback_table() -> None:
    engine = create_engine("sqlite:///:memory:")
    ChangePricingFeedback.__table__.create(engine)
    with Session(engine) as db:
        db.add(
            ChangePricingFeedback(
                analysis_key="deleted",
                subject_name="测试股份",
                subject_code="SZ000001",
                action="manual_delete",
                snapshot_json='{"subject_name":"测试股份"}',
            )
        )
        db.commit()
        assert db.scalar(select(ChangePricingFeedback)).analysis_key == "deleted"
