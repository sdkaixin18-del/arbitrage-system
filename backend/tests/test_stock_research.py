from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AStock, StockResearchEvidence, StockResearchVersion, StockResearchWorkspace
from app.stock_research import (
    ResearchEvidencePayload,
    ResearchWorkspacePayload,
    create_research_evidence,
    list_research_versions,
    restore_research_version,
    save_stock_research,
)


def test_stock_research_save_evidence_version_and_restore() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(AStock(code="000001", name="平安银行", exchange="SZ", full_code="SZ000001", pinyin="pinganyinhang"))
        db.commit()

        first = save_stock_research(
            "SZ000001",
            ResearchWorkspacePayload(
                research_status="跟踪中",
                current_conclusion="图形首次表达，等待基本面确认。",
                chart_expression="放量突破",
                fundamental_change="订单进入验证期",
                market_trading="市场交易业绩弹性",
                industry_position="上游关键环节",
                risks_and_invalidation="订单不兑现则证伪",
                next_validation="跟踪下一份公告",
                tag_names=["银行", "低估值"],
            ),
            db,
        )
        assert first["workspace"]["revision"] == 1
        assert first["tag_names"] == ["低估值", "银行"]

        created = create_research_evidence(
            "SZ000001",
            ResearchEvidencePayload(
                category="hard_fact",
                confirmation_status="已确认",
                title="公告验证",
                content="来自公司公告。",
                source_name="公司公告",
            ),
            db,
        )
        assert created["title"] == "公告验证"

        second = save_stock_research(
            "SZ000001",
            ResearchWorkspacePayload(
                research_status="重点跟踪",
                current_conclusion="硬事实与盘面形成共振。",
                chart_expression="趋势加速",
                fundamental_change="公告确认基本面变化",
                market_trading="市场交易估值重估",
                industry_position="中游核心供应商",
                risks_and_invalidation="需求转弱则证伪",
                next_validation="核对季度订单",
                tag_names=["银行"],
            ),
            db,
        )
        assert second["workspace"]["revision"] == 2

        history = list_research_versions("SZ000001", db)
        assert [item["revision"] for item in history["items"]] == [2, 1]
        assert len(history["items"][0]["snapshot"]["evidence"]) == 1

        restored = restore_research_version("SZ000001", history["items"][1]["id"], db)
        assert restored["workspace"]["revision"] == 3
        assert restored["workspace"]["current_conclusion"] == "图形首次表达，等待基本面确认。"
        assert restored["workspace"]["chart_expression"] == "放量突破"
        assert restored["workspace"]["fundamental_change"] == "订单进入验证期"
        assert restored["workspace"]["market_trading"] == "市场交易业绩弹性"
        assert restored["workspace"]["industry_position"] == "上游关键环节"
        assert restored["workspace"]["risks_and_invalidation"] == "订单不兑现则证伪"
        assert restored["workspace"]["next_validation"] == "跟踪下一份公告"
        assert restored["evidence"] == []
        assert db.scalar(select(StockResearchWorkspace)).revision == 3
        assert len(list(db.scalars(select(StockResearchVersion)).all())) == 3
        assert len(list(db.scalars(select(StockResearchEvidence)).all())) == 0
