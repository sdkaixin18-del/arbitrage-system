import json
import time
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app import industry_trends
from app.cross_market import _action_for_sector, cross_market_relation
from app.decision_review import _overview
from app.industry_trends import (
    CodexResult,
    CodexUpdateDelta,
    CatalystPayload,
    CompanyPayload,
    DraftPayload,
    EdgePayload,
    GeneratePayload,
    IndustryDecisionCreate,
    EvidenceStatePatch,
    MaterialCreate,
    MaterialPatch,
    NodePayload,
    ResearchSettingPayload,
    TrendCreate,
    TrendPatch,
    UpdatePayload,
    ValidationPayload,
    apply_draft,
    create_company,
    create_catalyst,
    create_edge,
    create_industry_decision,
    create_material,
    create_node,
    create_trend,
    create_update,
    create_validation,
    generate_trend,
    get_generation_job,
    get_industry_intelligence,
    get_research_settings,
    list_generation_jobs,
    list_versions,
    restore_version,
    save_draft,
    save_research_settings,
    update_trend,
    update_evidence_state,
    update_material,
    _codex_disabled_personal_skills_config,
    _codex_output_schema,
    _generation_error_summary,
)
from app.models import InformationScreeningBatch, InformationScreeningItem, IndustryChainEvidence, StockDailyBar


def test_attention_status_requires_multi_source_baseline() -> None:
    assert industry_trends._attention_status(3, 8, 2, 1, True) == ("样本不足", "不足")
    assert industry_trends._attention_status(20, 8, 12, 4, False) == ("基线待建立", "待建立")
    assert industry_trends._attention_status(20, 10, 12, 4, True) == ("信息扩散", "可用")
    assert industry_trends._attention_status(5, 10, 5, 3, True) == ("信息降温", "可用")
    assert industry_trends._attention_status(10, 9, 6, 3, True) == ("信息平稳", "可用")


def test_cross_market_relation_uses_previous_overseas_session() -> None:
    start = date(2026, 1, 1)
    overseas = []
    a_share = []
    for index in range(90):
        value = 0.012 if index % 5 in {0, 1, 2} else -0.006
        overseas.append((start + timedelta(days=index), value))
        a_share.append((start + timedelta(days=index + 1), value * 0.75 + (0.0005 if index % 2 else -0.0005)))
    result = cross_market_relation(overseas, a_share)
    assert result["aligned_samples"] == 60
    assert result["relation_state"] == "有效"
    assert result["correlation_60d"] is not None and result["correlation_60d"] > 0.9


def test_cross_market_action_never_treats_divergence_as_buy_signal() -> None:
    action, tone, reason = _action_for_sector("增强", "分化", "A股落后")
    assert action == "偏离观察"
    assert tone == "watch"
    assert "等待" in reason


def test_codex_output_schema_is_strict_at_every_object_level() -> None:
    def assert_strict(value) -> None:
        if isinstance(value, dict):
            assert "default" not in value
            if "properties" in value:
                assert value.get("additionalProperties") is False
                assert value.get("required") == list(value["properties"])
            for item in value.values():
                assert_strict(item)
        elif isinstance(value, list):
            for item in value:
                assert_strict(item)

    for model in (CodexResult, CodexUpdateDelta):
        schema = _codex_output_schema(model)
        assert_strict(schema)
        assert schema["$defs"]["CodexEdge"]["properties"] == {
            "from_name": {"title": "From Name", "type": "string"},
            "to_name": {"title": "To Name", "type": "string"},
        }


def test_generation_schema_error_is_returned_as_short_chinese_summary() -> None:
    raw = "ERROR: invalid_json_schema: 'additionalProperties' is required to be supplied and to be false"
    assert _generation_error_summary(raw) == "生成规则校验失败，请重新发起全面更新。"


def test_codex_generation_disables_personal_skills() -> None:
    config = _codex_disabled_personal_skills_config()
    assert config.startswith("skills.config=[")
    assert "/.codex/skills/qq/SKILL.md" in config
    assert "/.codex/skills/hithink-market-query/SKILL.md" in config
    assert "/.codex/skills/.system/" not in config


def test_industry_trend_crud_decisions_and_restore(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'industry-trend.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        trend = create_trend(TrendCreate(name="AI基础设施", summary="等待验证"), db)
        trend_id = trend["id"]
        assert trend["overall_verdict"] == "观察"

        passed = update_trend(
            trend_id,
            TrendPatch(
                summary="算力资本开支向上，重点验证订单兑现。",
                phase="增长期",
                attention_level="重点跟踪",
                direction_verdict="通过",
                stock_verdict="通过",
                timing_verdict="通过",
                pricing_status="部分定价",
            ),
            db,
        )
        assert passed["overall_verdict"] == "通过"
        assert any(item["affects_phase"] for item in passed["updates"])

        observed = update_trend(trend_id, TrendPatch(timing_verdict="观察"), db)
        assert observed["overall_verdict"] == "观察"
        rejected = update_trend(trend_id, TrendPatch(direction_verdict="否决", timing_verdict="通过"), db)
        assert rejected["overall_verdict"] == "否决"
        update_trend(trend_id, TrendPatch(direction_verdict="通过", timing_verdict="通过"), db)

        demand = create_node(trend_id, NodePayload(name="云厂商资本开支", node_type="需求驱动", plain_explanation="云厂商为AI数据中心增加预算", value_flow="资本开支转成服务器与网络订单", watch_signal="季度资本开支指引", maturity_status="放量中", sort_order=10), db)
        gpu = create_node(trend_id, NodePayload(name="算力服务器", node_type="网络与系统", sort_order=20), db)
        edge = create_edge(trend_id, EdgePayload(from_node_id=demand["id"], to_node_id=gpu["id"]), db)
        assert edge["from_node_id"] == demand["id"]

        first = create_company(
            trend_id,
            CompanyPayload(code="300308", name="中际旭创", node_ids=[gpu["id"]], is_primary=True, is_global_leader=True, tracking_status="核心受益"),
            db,
        )
        second = create_company(
            trend_id,
            CompanyPayload(
                code="002463",
                name="沪电股份",
                node_ids=[gpu["id"]],
                is_primary=True,
                is_domestic_alternative=True,
                tracking_status="重点跟踪",
                market_implied_expectation="当前市值已经反映AI PCB需求增长，但仍要求高端板收入继续放量。",
                evidence_based_expectation="订单与产品结构能够支持收入增长，利润兑现仍待中报确认。",
                expectation_gap_status="正向预期差",
                expectation_gap_reason="市场尚未完全交易高端板占比和毛利提升。",
                expectation_trigger="中报确认高端板收入和毛利率同步提升。",
                expectation_invalidation="收入增长但毛利率和现金流持续下降。",
                expectation_as_of=date(2026, 7, 19),
                expectation_anchor_market_cap=110_000_000_000,
            ),
            db,
        )
        current = industry_trends.get_trend(trend_id, db)
        assert current["primary_company"]["id"] == second["id"]
        assert sum(1 for item in current["companies"] if item["is_primary"]) == 1
        assert next(item for item in current["companies"] if item["id"] == first["id"])["is_primary"] is False
        second_detail = next(item for item in current["companies"] if item["id"] == second["id"])
        assert second_detail["expectation_gap_status"] == "正向预期差"
        assert second_detail["expectation_anchor_market_cap"] == 110_000_000_000
        assert second_detail["market_implied_expectation"].startswith("当前市值")

        global_company = create_company(
            trend_id,
            CompanyPayload(
                code="COHR",
                name="Coherent",
                market="美股",
                exchange="NYSE",
                external_url="https://www.coherent.com/",
                node_ids=[gpu["id"]],
                is_global_leader=True,
                tracking_status="观察",
            ),
            db,
        )
        assert global_company["full_code"] == "US:COHR"
        assert global_company["market"] == "美股"
        with pytest.raises(HTTPException) as invalid_domestic:
            create_company(
                trend_id,
                CompanyPayload(code="LITE", name="Lumentum", market="美股", is_domestic_alternative=True),
                db,
            )
        assert invalid_domestic.value.status_code == 400
        current = industry_trends.get_trend(trend_id, db)
        assert {item["name"] for item in current["company_recommendations"]["global_leaders"]} == {"中际旭创", "Coherent"}
        assert [item["name"] for item in current["company_recommendations"]["domestic_alternatives"]] == ["沪电股份"]

        validation = create_validation(
            trend_id,
            ValidationPayload(
                name="云厂商资本开支是否继续增长",
                criteria="同比增长且指引上调",
                company_id=second["id"],
                node_id=demand["id"],
                status="验证中",
                target_date=date(2026, 10, 31),
            ),
            db,
        )
        assert validation["company_id"] == second["id"]
        catalyst = create_catalyst(
            trend_id,
            CatalystPayload(
                event_name="海外云厂商资本开支指引",
                expected_time="2026-Q3",
                event_type="资本开支",
                impact_node_id=demand["id"],
                impact_company_id=second["id"],
                importance="高",
                status="预期",
                impact="验证需求能否继续向订单传导",
                sort_order=10,
            ),
            db,
        )
        assert catalyst["impact_node_id"] == demand["id"]
        create_validation(
            trend_id,
            ValidationPayload(name="产品价格能否维持", status="失败", current_result="价格连续回落"),
            db,
        )
        after_failed_validation = industry_trends.get_trend(trend_id, db)
        assert after_failed_validation["direction_verdict"] == "通过"
        assert any("验证指标失败" in item["content"] for item in after_failed_validation["updates"])
        create_update(
            trend_id,
            UpdatePayload(
                update_date=date.today() - timedelta(days=1),
                content="海外云厂商上调资本开支",
                source_name="公司公告",
                source_url="https://example.com/source",
                causal_stage="真实变化",
                evidence_type="硬事实",
                signal_status="已确认",
                buyer_group="产业资金与机构",
                market_response="同链公司放量上行",
                sell_pressure="中",
                counter_evidence="估值已经明显抬升",
                affects_decision=True,
            ),
            db,
        )

        before_change = industry_trends.get_trend(trend_id, db)
        saved_revision = before_change["revision"]
        saved_summary = before_change["summary"]
        update_trend(trend_id, TrendPatch(summary="临时改坏的结论", direction_verdict="否决"), db)
        version = next(item for item in list_versions(trend_id, db)["items"] if item["revision"] == saved_revision)
        restored = restore_version(trend_id, version["id"], db)
        assert restored["summary"] == saved_summary
        assert restored["overall_verdict"] == "通过"
        assert len(restored["nodes"]) == 2
        restored_demand = next(item for item in restored["nodes"] if item["name"] == "云厂商资本开支")
        assert restored_demand["plain_explanation"] == "云厂商为AI数据中心增加预算"
        assert restored_demand["maturity_status"] == "放量中"
        assert len(restored["edges"]) == 1
        assert len(restored["companies"]) == 3
        restored_global = next(item for item in restored["companies"] if item["code"] == "COHR")
        assert restored_global["market"] == "美股"
        assert restored_global["external_url"] == "https://www.coherent.com/"
        assert restored_global["is_global_leader"] is True
        assert restored["company_recommendations"]["domestic_alternatives"][0]["name"] == "沪电股份"
        assert len(restored["catalysts"]) == 1
        assert restored["catalysts"][0]["event_name"] == "海外云厂商资本开支指引"
        restored_signal = next(item for item in restored["updates"] if item["content"] == "海外云厂商上调资本开支")
        assert restored_signal["causal_stage"] == "真实变化"
        assert restored_signal["evidence_type"] == "硬事实"
        assert restored_signal["buyer_group"] == "产业资金与机构"
        assert restored_signal["sell_pressure"] == "中"
        # 一般验证失败只进入风险提示；没有行情确认时不能永久“一票否决”为风险收缩。
        assert restored["dynamics"]["action"] == "只跟踪，不追"
        assert restored["dynamics"]["evidence"]["hard_facts"] == 1
        assert restored["dynamics"]["evidence"]["confirmed"] == 1
        assert "关键验证" in restored["dynamics"]["decision_trace"]["waiting"]
        assert "breadth_5d_pct" in restored["dynamics"]["market"]
        assert restored["dynamics"]["freshness"]["market"] is None
        linked_validation = next(item for item in restored["validations"] if item["name"] == "云厂商资本开支是否继续增长")
        assert linked_validation["company_id"] is not None
        assert linked_validation["node_id"] is not None


def test_industry_intelligence_deduplicates_sources_and_links_subsectors(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'industry-intelligence.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        optical = create_trend(TrendCreate(name="光通信（AI数据中心光互联）", summary="带宽升级"), db)
        pcb = create_trend(TrendCreate(name="PCB（AI服务器与高速交换）", summary="板级价值量提升"), db)
        space = create_trend(TrendCreate(name="商业航天", summary="可复用火箭与卫星互联网"), db)
        optical_node = create_node(
            optical["id"],
            NodePayload(name="光模块", node_type="光互联产品", investment_importance="强"),
            db,
        )
        create_company(
            optical["id"],
            CompanyPayload(
                code="300308",
                name="中际旭创",
                node_ids=[optical_node["id"]],
                tracking_status="核心受益",
                expectation_gap_status="正向预期差",
                market_implied_expectation="当前市值隐含高增长继续。",
                evidence_based_expectation="订单证据支持增长。",
                expectation_gap_reason="订单强于当前一致预期。",
            ),
            db,
        )
        create_validation(
            optical["id"],
            ValidationPayload(name="云厂商资本开支是否增长", status="已确认"),
            db,
        )
        create_validation(
            pcb["id"],
            ValidationPayload(name="高端PCB订单是否增长", status="失败", current_result="订单暂未兑现"),
            db,
        )
        shared_url = "https://example.com/ai-capex"
        db.add(
            IndustryChainEvidence(
                chain_id=optical["id"],
                title="云厂商上调AI资本开支",
                source_name="公司财报",
                source_url=shared_url,
                evidence_date=date(2026, 7, 16),
                source_tier="公司披露",
                verification_status="已互证",
                evidence_state="有效",
                impact_level="强",
            )
        )
        batch = InformationScreeningBatch(batch_key="ai-fresh-test", title="AI产业增量")
        db.add(batch)
        db.flush()
        db.add(
            InformationScreeningItem(
                batch_id=batch.id,
                item_key="ai-capex-shared-change",
                title="云厂商AI资本开支同时拉动光通信与PCB",
                summary="光模块和高端PCB订单共同受益。",
                source_name="公司财报",
                source_url="https://example.com/latest-ai-capex",
                published_at=datetime.now(),
                bucket="verified",
                importance=5,
                verification_status="cross_verified",
                related_sectors_json='["光通信", "PCB"]',
                related_stocks_json='["中际旭创"]',
                price_status="partial",
            )
        )
        db.add(
            InformationScreeningItem(
                batch_id=batch.id,
                item_key="filtered-duplicate-opinion",
                title="光模块与PCB重复观点",
                summary="已在上一批出现，不构成新变化。",
                source_name="调研信息",
                published_at=datetime.now(),
                bucket="filtered",
                importance=5,
                verification_status="unverified",
                related_sectors_json='["光通信", "PCB"]',
            )
        )
        db.commit()

        result = get_industry_intelligence(db)
        assert result["theme"] == "A股产业链"
        assert result["summary"]["industries"] == 3
        assert result["summary"]["sources"] == 1
        assert result["summary"]["supporting_evidence"] == 1
        assert result["summary"]["shared_evidence"] == 1
        assert result["summary"]["confirmed_validations"] == 1
        assert result["summary"]["risk_evidence"] == 1
        assert result["summary"]["fresh_changes_7d"] == 1
        assert result["summary"]["verified_changes_7d"] == 1
        assert result["summary"]["shared_changes_7d"] == 1
        assert result["overall"]["action"] == "等待触发"
        assert result["fresh_changes"][0]["is_shared"] is True
        assert result["fresh_changes"][0]["auto_tier"] == "硬证据"
        assert result["fresh_changes"][0]["auto_action"] == "已自动计入产业硬证据"
        assert {item["name"] for item in result["fresh_changes"][0]["affected_nodes"]} == {"光模块"}
        assert {item["name"] for item in result["fresh_changes"][0]["affected_companies"]} == {"中际旭创"}
        assert all(item["item_key"] != "filtered-duplicate-opinion" for item in result["fresh_changes"])
        assert {item["name"] for item in result["fresh_changes"][0]["affected_industries"]} == {
            "光通信（AI数据中心光互联）",
            "PCB（AI服务器与高速交换）",
        }
        assert all(
            "商业航天" not in {industry["name"] for industry in item["affected_industries"]}
            for item in result["supporting_evidence"]
            if item.get("association_reason") == "共同需求"
        )
        assert all("action" in item and "market" in item for item in result["sectors"])
        assert all("company_recommendations" in item for item in result["sectors"])
        optical_sector = next(item for item in result["sectors"] if item["id"] == optical["id"])
        assert optical_sector["sector_expectation"]["status"] == "一致预期待补"
        assert "不代表预期差" in optical_sector["sector_expectation"]["summary"]
        pcb_sector = next(item for item in result["sectors"] if item["id"] == pcb["id"])
        assert pcb_sector["sector_expectation"]["consensus_count"] == 0
        assert {item["name"] for item in result["latest_changes"][0]["affected_industries"]} == {
            "光通信（AI数据中心光互联）",
            "PCB（AI服务器与高速交换）",
        }
        assert next(item for item in result["validation_route"] if item["stage"] == "需求")["confirmed"] == 1
        assert any(item["signal_type"] == "验证失败" for item in result["risk_signals"])


def test_information_association_spreads_shared_ai_demand_to_sibling_industries(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'shared-demand.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        optical = create_trend(TrendCreate(name="光通信（AI数据中心光互联）", summary="AI数据中心带宽升级"), db)
        pcb = create_trend(TrendCreate(name="PCB（AI服务器与高速交换）", summary="AI服务器板级升级"), db)
        batch = InformationScreeningBatch(batch_key="shared-demand", title="共同需求")
        db.add(batch)
        db.flush()
        row = InformationScreeningItem(
            batch_id=batch.id,
            item_key="enterprise-ai-budget",
            title="企业AI预算向云平台和算力集中",
            summary="云厂商资本开支继续增加。",
            published_at=datetime.now(),
            bucket="verified",
            verification_status="cross_verified",
            related_sectors_json='["企业AI", "云计算", "服务器"]',
        )

        for trend_id in (optical["id"], pcb["id"]):
            chain = db.get(industry_trends.IndustryChain, trend_id)
            association = industry_trends._information_association(row, chain, [], [])
            assert association["matched"] is True
            assert any(reason.startswith("共同需求：") for reason in association["reasons"])


def test_codex_generation_only_creates_draft_until_user_applies(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'industry-generation.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(industry_trends, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False))

    generated = {
        "summary": "AI资本开支扩张仍在验证。",
        "investment_logic": "算力需求向服务器和高速互联传导。",
        "change_summary": "云厂商提高资本开支。",
        "why_now": "模型训练和推理需求同步增长。",
        "drivers": ["需求", "资本开支"],
        "expected_duration": "2-3年",
        "risk": "资本开支下调",
        "phase": "验证期",
        "strength": 76,
        "attention_level": "重点跟踪",
        "direction_verdict": "通过",
        "stock_verdict": "观察",
        "timing_verdict": "观察",
        "pricing_status": "部分定价",
        "priced_in": "龙头估值已有反映",
        "not_priced_in": "订单兑现速度",
        "next_signal": "季度订单上调",
        "invalidation": "资本开支连续下调",
        "nodes": [
            {"name": "云厂商需求", "node_type": "需求驱动", "plain_explanation": "云厂商增加AI预算"},
            {"name": "高速互联", "node_type": "光互联产品", "tech_barrier": "强", "maturity_status": "放量中"},
        ],
        "edges": [{"from_name": "云厂商需求", "to_name": "高速互联"}],
        "companies": [
            {
                "code": "300308",
                "name": "中际旭创",
                "node_names": ["高速互联"],
                "tracking_status": "核心受益",
                "verification_status": "验证中",
                "pricing_status": "部分定价",
                "is_primary": True,
            }
        ],
        "validations": [{"name": "订单是否增长", "status": "验证中", "node_name": "高速互联", "company_code": "300308"}],
        "updates": [{"update_date": "2026-07-14", "content": "首次研究"}],
        "sources": [
            {
                "title": "资本开支公告",
                "source_name": "公司官网",
                "source_url": "https://example.com/official",
                "evidence_date": "2026-07-14",
                "impact_level": "强",
                "source_tier": "官方硬证据",
                "verification_status": "已互证",
                "node_names": ["云厂商需求", "高速互联"],
            }
        ],
    }
    monkeypatch.setenv("INDUSTRY_TREND_CODEX_TEST_RESULT", json.dumps(generated, ensure_ascii=False))

    with Session(engine) as db:
        job = generate_trend(GeneratePayload(name="AI基础设施", job_type="initial"), db)
        for _ in range(100):
            time.sleep(0.02)
            db.expire_all()
            current = get_generation_job(job["id"], db)
            if current["status"] not in {"queued", "running"}:
                break
        assert current["status"] == "succeeded"

        before_apply = industry_trends.get_trend(job["chain_id"], db)
        assert before_apply["summary"] is None
        assert before_apply["draft"]["payload"]["summary"] == generated["summary"]

        applied = apply_draft(job["chain_id"], db)
        assert applied["summary"] == generated["summary"]
        assert applied["draft"] is None
        assert applied["overall_verdict"] == "观察"
        assert applied["primary_company"]["name"] == "中际旭创"
        assert len(applied["sources"]) == 1
        assert applied["sources"][0]["source_tier"] == "官方硬证据"
        assert applied["sources"][0]["verification_status"] == "已互证"
        assert len(applied["sources"][0]["node_ids"]) == 2


def test_full_update_uses_saved_settings_and_merges_delta_without_deleting_existing_data(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'industry-update-delta.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        trend = create_trend(TrendCreate(name="光通信", summary="原有结论"), db)
        trend_id = trend["id"]
        old_node = create_node(
            trend_id,
            NodePayload(name="800G光模块", node_type="光互联产品", plain_explanation="原有节点", investment_importance="强"),
            db,
        )
        create_company(
            trend_id,
            CompanyPayload(code="300308", name="中际旭创", node_ids=[old_node["id"]], tracking_status="核心受益"),
            db,
        )

        saved = save_research_settings(
            trend_id,
            ResearchSettingPayload(
                priority_nodes=["OCS"],
                priority_companies=["中际旭创"],
                excluded_keywords=["营销稿"],
                token_budget=16000,
                allow_new_nodes=True,
                allow_new_companies=True,
            ),
            db,
        )
        assert saved["update_mode"] == "全面更新"
        assert saved["priority_nodes"] == ["OCS"]
        assert saved["draft_only"] is True
        assert get_research_settings(trend_id, db)["token_budget"] == 16000

        delta = {
            "delta_version": "1",
            "no_material_change": False,
            "update_summary": "Google OCS进入AI集群验证阶段",
            "core_patch": {"change_summary": "OCS成为新增验证方向"},
            "coverage": [
                {"area": "技术路线", "status": "已更新", "finding": "OCS用于动态拓扑"},
                {"area": "订单与利润", "status": "证据不足", "gap": "等待外部客户订单"},
            ],
            "nodes": [
                {
                    "action": "新增",
                    "name": "OCS光路交换",
                    "node_type": "网络与系统",
                    "plain_explanation": "动态改变光纤连接",
                    "investment_importance": "强",
                }
            ],
            "edges_add": [{"from_name": "OCS光路交换", "to_name": "800G光模块"}],
            "companies": [],
            "validations": [
                {
                    "action": "新增",
                    "name": "OCS是否出现外部客户",
                    "criteria": "披露非自用客户或批量订单",
                    "status": "未验证",
                    "node_name": "OCS光路交换",
                }
            ],
            "catalysts": [],
            "updates": [],
            "new_sources": [
                {
                    "title": "Google Jupiter OCS",
                    "source_name": "Google Research",
                    "source_url": "https://research.google/ocs",
                    "evidence_date": "2026-07-15",
                    "impact_level": "强",
                    "source_tier": "官方硬证据",
                    "verification_status": "单一来源",
                    "node_names": ["OCS光路交换"],
                }
            ],
        }
        save_draft(trend_id, DraftPayload(draft_type="update", source="codex_chat", payload=delta), db)
        before = industry_trends.get_trend(trend_id, db)
        assert len(before["nodes"]) == 1
        applied = apply_draft(trend_id, db)

        assert applied["draft"] is None
        assert applied["change_summary"] == "OCS成为新增验证方向"
        assert {item["name"] for item in applied["nodes"]} == {"800G光模块", "OCS光路交换"}
        assert len(applied["companies"]) == 1
        assert applied["companies"][0]["name"] == "中际旭创"
        assert len(applied["edges"]) == 1
        assert any(item["name"] == "OCS是否出现外部客户" for item in applied["validations"])
        assert any(item["source_url"] == "https://research.google/ocs" for item in applied["sources"])


def test_full_update_job_records_delta_and_token_estimates(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'industry-update-job.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(industry_trends, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False))
    generated = {
        "delta_version": "1",
        "no_material_change": True,
        "update_summary": "全面检查完成，暂无实质变化",
        "core_patch": {},
        "coverage": [
            {"area": "需求与资本开支", "status": "无变化"},
            {"area": "价格与订单", "status": "证据不足", "gap": "等待季度数据"},
        ],
        "invalidated_information": [],
        "contradictions": [],
        "nodes": [],
        "edges_add": [],
        "companies": [],
        "validations": [],
        "catalysts": [],
        "updates": [],
        "new_sources": [],
    }
    monkeypatch.setenv("INDUSTRY_TREND_CODEX_TEST_RESULT", json.dumps(generated, ensure_ascii=False))

    with Session(engine) as db:
        trend = create_trend(TrendCreate(name="光通信全面更新"), db)
        create_node(trend["id"], NodePayload(name="800G光模块", node_type="光互联产品"), db)
        material = create_material(
            trend["id"],
            MaterialCreate(title="OCS新增线索", content="等待全面更新核验", source_name="本地对话"),
            db,
        )
        job = generate_trend(GeneratePayload(chain_id=trend["id"], job_type="update"), db)
        for _ in range(100):
            time.sleep(0.02)
            db.expire_all()
            current = get_generation_job(job["id"], db)
            if current["status"] not in {"queued", "running"}:
                break
        assert current["status"] == "succeeded"
        assert current["update_mode"] == "全面更新"
        assert current["input_token_estimate"] > 0
        assert current["output_token_estimate"] > 0
        assert current["change_count"] == 0
        assert list_generation_jobs(trend["id"], 20, db)["items"][0]["id"] == job["id"]
        assert get_research_settings(trend["id"], db)["last_researched_at"] is not None
        draft = industry_trends.get_trend(trend["id"], db)["draft"]
        assert draft["payload"]["delta_version"] == "1"
        assert draft["payload"]["_material_ids"] == [material["id"]]
        applied = apply_draft(trend["id"], db)
        assert next(item for item in applied["materials"] if item["id"] == material["id"])["status"] == "已纳入"
        db.expire_all()
        assert get_generation_job(job["id"], db)["phase"] == "已应用"


def test_material_queue_source_state_and_industry_decision_review(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'industry-decision-loop.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        trend = create_trend(TrendCreate(name="光通信决策闭环", summary="AI资本开支向光互联传导"), db)
        trend_id = trend["id"]
        update_trend(
            trend_id,
            TrendPatch(
                direction_verdict="通过",
                stock_verdict="通过",
                timing_verdict="观察",
                pricing_status="部分定价",
                next_signal="订单与业绩继续兑现",
                invalidation="云厂商资本开支连续下调",
            ),
            db,
        )
        company = create_company(
            trend_id,
            CompanyPayload(
                code="300308",
                name="中际旭创",
                is_primary=True,
                primary_reason="订单与利润兑现路径最直接",
                tracking_status="核心受益",
            ),
            db,
        )

        material = create_material(
            trend_id,
            MaterialCreate(
                title="云厂商资本开支更新",
                source_name="本地对话",
                content="新增订单验证线索",
                change_type="新增证据",
            ),
            db,
        )
        assert material["status"] == "待处理"
        with pytest.raises(HTTPException) as duplicate:
            create_material(
                trend_id,
                MaterialCreate(
                    title="云厂商资本开支更新",
                    source_name="本地对话",
                    content="重复材料",
                    change_type="新增证据",
                ),
                db,
            )
        assert getattr(duplicate.value, "status_code", None) == 409
        ignored = update_material(trend_id, material["id"], MaterialPatch(status="忽略", note="内容重复"), db)
        assert ignored["status"] == "忽略"
        assert ignored["processed_at"] is not None

        evidence = IndustryChainEvidence(
            chain_id=trend_id,
            company_id=company["id"],
            title="客户订单公告",
            source_name="公司公告",
            source_url="https://example.com/order",
            evidence_date=date.today(),
        )
        db.add(evidence)
        db.commit()
        db.refresh(evidence)
        conflicted = update_evidence_state(
            trend_id,
            evidence.id,
            EvidenceStatePatch(evidence_state="存在冲突", conflict_note="与客户口径不一致"),
            db,
        )
        assert conflicted["evidence_state"] == "存在冲突"
        assert conflicted["conflict_note"] == "与客户口径不一致"

        for offset in range(1, 61):
            price = 100 + offset
            db.add(
                StockDailyBar(
                    code="300308",
                    name="中际旭创",
                    exchange="SZ",
                    full_code="SZ300308",
                    trade_date=date.today() + timedelta(days=offset),
                    open=price,
                    high=price * 1.02,
                    low=price * 0.98,
                    close=price + 0.5,
                    change_pct=0.5,
                    amount=1_000_000_000,
                    source="test",
                )
            )
        db.commit()

        created = create_industry_decision(
            trend_id,
            IndustryDecisionCreate(
                decision_code="B",
                primary_company_id=company["id"],
                planned_horizon=20,
                thesis="产业方向已确认，等待时点进一步确认",
            ),
            db,
        )
        decision = created["item"]
        assert decision["source_kind"] == "industry_trend"
        assert decision["primary_full_code"] == "SZ300308"
        assert decision["execution_status"] == "shadow_matured"
        assert decision["performance"]["horizons"]["20"]["available"] is True

        overview = _overview(db)
        assert overview["summary"]["pipeline"]["industry_decision_total"] == 1
        assert overview["items"][0]["industry_chain_id"] == trend_id
        assert overview["items"][0]["source_kind"] == "industry_trend"
