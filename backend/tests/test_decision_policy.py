from app.decision_policy import evaluate_industry_policies, evaluate_information_policies


def _recommendations(result: dict) -> dict[str, str]:
    return {
        policy["policy_id"]: policy["recommendation"]
        for policy in result["policies"]
    }


def test_information_policy_keeps_qq_as_a_comparable_hypothesis() -> None:
    result = evaluate_information_policies(
        {
            "bucket": "verified",
            "verification_status": "verified",
            "published_at": "2026-07-23T09:00:00+08:00",
            "source_url": "https://example.com/official",
            "marginal_change": "订单首次得到正式确认",
            "related_stocks": ["测试股份 688800"],
            "price_status": "first_expression",
            "price_summary": "首次放量，尚未形成多轮交易",
            "invalidation_conditions": ["订单后续被撤销"],
        }
    )

    assert _recommendations(result) == {"qq_2_1": "C", "simple_baseline": "A"}
    assert result["disagreement"] is True
    assert result["quality_complete"] is True
    assert "不自动覆盖" in result["principle"]


def test_disproved_information_is_rejected_by_both_policies() -> None:
    result = evaluate_information_policies(
        {
            "bucket": "filtered",
            "verification_status": "disproved",
            "price_status": "unknown",
        }
    )

    assert _recommendations(result) == {"qq_2_1": "D", "simple_baseline": "D"}
    assert result["disagreement"] is False


def test_industry_policy_can_pass_when_all_current_qq_gates_are_present() -> None:
    company = {
        "name": "测试股份",
        "verification_status": "已确认",
        "benefit_directness": "强",
        "profit_path": "订单增长转化为利润",
        "primary_reason": "同链订单兑现更快",
        "expectation_gap_status": "正向预期差",
        "market_implied_expectation": "市场只计入存量业务",
        "evidence_based_expectation": "新增订单支持下一年盈利增长",
        "expectation_as_of": "2026-07-23",
        "expectation_anchor_market_cap": 100_000_000_000,
    }
    result = evaluate_industry_policies(
        {
            "last_change_at": "2026-07-23T09:00:00+08:00",
            "change_summary": "新订单进入批量交付",
            "direction_verdict": "通过",
            "stock_verdict": "通过",
            "timing_verdict": "通过",
            "pricing_status": "未定价",
            "invalidation": "交付连续两个季度低于计划",
            "sources": [
                {
                    "evidence_state": "有效",
                    "source_tier": "官方硬证据",
                    "verification_status": "已互证",
                    "source_name": "交易所公告",
                    "title": "订单公告",
                },
                {
                    "evidence_state": "有效",
                    "source_tier": "公司披露",
                    "verification_status": "单一来源",
                    "source_name": "公司业绩会",
                    "title": "业绩会纪要",
                },
            ],
            "dynamics": {"expression": {"market_confirmed": True}},
        },
        company,
    )

    assert _recommendations(result) == {"qq_2_1": "A", "simple_baseline": "A"}
    assert result["disagreement"] is False
    assert result["quality_complete"] is True


def test_industry_policy_accepts_existing_reviewed_official_vocabulary() -> None:
    result = evaluate_industry_policies(
        {
            "last_change_at": "2026-07-23T09:00:00+08:00",
            "change_summary": "正式业绩预告首次确认放量",
            "direction_verdict": "通过",
            "stock_verdict": "通过",
            "timing_verdict": "观察",
            "pricing_status": "部分定价",
            "invalidation": "正式财报不支持预告",
            "sources": [
                {
                    "evidence_state": "有效",
                    "source_tier": "官方硬证据",
                    "verification_status": "已复核",
                    "source_name": "公司公告",
                },
                {
                    "evidence_state": "有效",
                    "source_tier": "公司官方",
                    "verification_status": "已复核",
                    "source_name": "公司官网",
                },
            ],
        },
        {
            "name": "测试股份",
            "verification_status": "已确认",
            "benefit_directness": "直接",
            "profit_path": "订单转收入",
            "primary_reason": "公告证据更直接",
            "expectation_gap_status": "无法判断",
        },
    )

    qq = result["policies"][0]
    assert [gate["state"] for gate in qq["gates"][:3]] == ["pass", "pass", "pass"]
    assert qq["recommendation"] == "B"
