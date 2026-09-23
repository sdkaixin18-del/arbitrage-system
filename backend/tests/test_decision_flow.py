from datetime import date

import pandas as pd

from app.decision_flow import _rank_discovery_candidates, build_decision_flow_payload


def _market(*, market_confirmed: bool = True) -> dict:
    return {
        "as_of": "2026-07-23",
        "state": "防守占优",
        "risk_budget": "0–0.3x",
        "action": "总风险保持低位。",
        "metrics": {
            "market_member_breadth": 0.38,
            "industry_index_breadth": 0.20,
            "high_low_beta_spread_20d": -0.20,
        },
        "discovery_count": 1,
        "discovery_qualified_count": 1,
        "market_confirmed_count": int(market_confirmed),
        "candidates": [
            {
                "group_code": "881156",
                "name": "通信服务",
                "rank": 1,
                "return_5d_pct": 3.0,
                "return_20d_pct": 8.0,
                "breadth_pct": 80.0,
                "amount_ratio": 1.2,
                "persistence_days": 8,
                "discovery_gate_count": 3,
                "confirmation_gate_count": 2 if market_confirmed else 1,
                "discovery_qualified": True,
                "market_confirmed": market_confirmed,
                "discovery_gates": [
                    {
                        "key": "relative_strength",
                        "label": "相对强度",
                        "layer": "discover",
                        "status": "pass",
                    },
                    {
                        "key": "turnover",
                        "label": "成交确认",
                        "layer": "discover",
                        "status": "pass",
                    },
                    {
                        "key": "persistence",
                        "label": "持续性",
                        "layer": "discover",
                        "status": "pass",
                    },
                ],
                "confirmation_gates": [
                    {
                        "key": "breadth",
                        "label": "板块广度",
                        "layer": "confirm",
                        "status": "pass",
                    },
                    {
                        "key": "diffusion",
                        "label": "龙头扩散",
                        "layer": "confirm",
                        "status": "pass" if market_confirmed else "wait",
                    },
                ],
            }
        ],
    }


def _sector(**overrides) -> dict:
    payload = {
        "id": 5,
        "name": "云计算与算力租赁（AI云服务）",
        "phase": "验证期",
        "direction_verdict": "通过",
        "pricing_status": "部分定价",
        "primary_company": "中国电信",
        "action": "可进入交易计划",
        "action_tone": "positive",
        "action_reason": "硬变化、直接传导和盘面表达同时成立。",
        "evidence_status": "硬变化已确认",
        "evidence_summary": {"hard": 4, "mutual": 2, "risk": 0},
        "expression": {"verified": True, "market_confirmed": True},
        "market": {"status": "龙头先行"},
        "next_signal": "等待下一份经营数据",
        "invalidation": "出租率和现金流连续转弱",
    }
    payload.update(overrides)
    return payload


def test_discovery_confirmation_and_information_are_required_for_a_queue() -> None:
    result = build_decision_flow_payload(
        _market(),
        {"sectors": [_sector()]},
        today=date(2026, 7, 24),
    )

    assert [item["name"] for item in result["queues"]["A"]] == ["云计算与算力租赁（AI云服务）"]
    assert result["headline"]["risk_budget"] == "0–0.3x"
    assert "0–0.3x" in result["headline"]["action"]


def test_missing_hard_information_downgrades_market_expression_to_c() -> None:
    result = build_decision_flow_payload(
        _market(),
        {"sectors": []},
        today=date(2026, 7, 24),
    )

    assert result["counts"] == {"A": 0, "B": 0, "C": 1, "D": 0}
    candidate = result["queues"]["C"][0]
    assert candidate["name"] == "通信服务"
    assert next(gate for gate in candidate["gates"] if gate["key"] == "information")["status"] == "wait"
    assert [gate["key"] for gate in candidate["gates"] if gate["layer"] == "discover"] == [
        "relative_strength",
        "turnover",
        "persistence",
    ]
    assert all(gate["key"] != "style" for gate in candidate["gates"])


def test_valid_direction_with_unconfirmed_expression_stays_in_b() -> None:
    result = build_decision_flow_payload(
        _market(market_confirmed=False),
        {"sectors": [_sector(expression={"verified": True, "market_confirmed": False}, action_tone="watch")]},
        today=date(2026, 7, 24),
    )

    assert [item["name"] for item in result["queues"]["B"]] == ["云计算与算力租赁（AI云服务）"]
    assert not result["queues"]["A"]


def test_decision_flow_does_not_publish_a_combined_score() -> None:
    result = build_decision_flow_payload(
        _market(),
        {"sectors": [_sector()]},
        today=date(2026, 7, 24),
    )

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert "effect_score" not in set(keys(result))
    assert "total_score" not in set(keys(result))


def test_flow_exposes_exactly_three_layers_and_style_only_controls_position() -> None:
    result = build_decision_flow_payload(
        _market(),
        {"sectors": [_sector()]},
        today=date(2026, 7, 24),
    )

    assert [step["key"] for step in result["steps"]] == ["discover", "confirm", "position"]
    assert result["steps"][0]["detail"] == "强势 + 量能 + 持续性，只生成 Top 5"
    assert "不改变行业排序" in result["steps"][2]["detail"]
    assert result["rule"] == "强势、量能、持续性负责发现；广度、扩散和硬信息负责确认；市场风格只控制仓位。"


def test_top_five_ranking_ignores_confirmation_and_market_style() -> None:
    rows = []
    for index in range(6):
        rows.append(
            {
                "group_name": f"方向{index + 1}",
                "discovery_qualified": index != 5,
                "discovery_gate_count": 3 if index != 5 else 2,
                "top_quartile_days_last_10": 10 - index,
                "return_20d_rank": 0.9 - index * 0.05,
                "amount_ratio_5d_20d": 1.5 - index * 0.05,
                "market_confirmed": index == 5,
                "beta_rank": 1.0 if index == 5 else 0.1,
            }
        )

    ranked = _rank_discovery_candidates(pd.DataFrame(rows))

    assert ranked["group_name"].tolist() == ["方向1", "方向2", "方向3", "方向4", "方向5"]
    assert "方向6" not in ranked["group_name"].tolist()
