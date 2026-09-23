from __future__ import annotations

from typing import Any, Literal


DecisionCode = Literal["A", "B", "C", "D"]
GateState = Literal["pass", "wait", "fail", "missing"]

QQ_POLICY_ID = "qq_2_1"
QQ_POLICY_VERSION = "qq-2.1-experimental-2026-07-23"
BASELINE_POLICY_ID = "simple_baseline"
BASELINE_POLICY_VERSION = "simple-baseline-2026-07-23"

GATE_LABELS = {
    "evidence": "证据",
    "industry": "产业确认",
    "stock": "股票表达",
    "pricing": "定价",
    "timing": "时点",
}


def _gate(key: str, state: GateState, summary: str, *, facts: list[str] | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "label": GATE_LABELS[key],
        "state": state,
        "summary": summary,
        "facts": facts or [],
    }


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def _traceable_information(item: dict[str, Any]) -> bool:
    return bool(
        item.get("source_url")
        or _as_list(item.get("source_urls"))
        or item.get("official_source_url")
    )


def _qq_code(gates: list[dict[str, Any]]) -> DecisionCode:
    state = {item["key"]: item["state"] for item in gates}
    if any(state.get(key) == "fail" for key in ("evidence", "industry", "stock")):
        return "D"
    if any(state.get(key) != "pass" for key in ("evidence", "industry", "stock")):
        return "C"
    if state.get("pricing") != "pass" or state.get("timing") != "pass":
        return "B"
    return "A"


def _policy(
    *,
    policy_id: str,
    version: str,
    label: str,
    recommendation: DecisionCode,
    reason: str,
    gates: list[dict[str, Any]] | None = None,
    scope: str,
) -> dict[str, Any]:
    return {
        "policy_id": policy_id,
        "version": version,
        "label": label,
        "recommendation": recommendation,
        "reason": reason,
        "scope": scope,
        "gates": gates or [],
    }


def _quality_checks_for_information(item: dict[str, Any]) -> list[dict[str, Any]]:
    checks = [
        ("time_frozen", "首次时间", bool(item.get("published_at")), "缺少首次可得时间"),
        ("source_traceable", "原始来源", _traceable_information(item), "缺少可追溯原始链接"),
        ("change_explicit", "边际变化", _nonempty(item.get("marginal_change")), "没有写清相对旧认知新增了什么"),
        ("pricing_explicit", "价格口径", _nonempty(item.get("price_summary")), "没有写清价格已经反映什么"),
        (
            "invalidation_explicit",
            "证伪条件",
            bool(_as_list(item.get("invalidation_conditions"))),
            "缺少可执行证伪条件",
        ),
    ]
    return [
        {"key": key, "label": label, "passed": passed, "message": "已记录" if passed else missing}
        for key, label, passed, missing in checks
    ]


def evaluate_information_policies(item: dict[str, Any]) -> dict[str, Any]:
    verification = str(item.get("verification_status") or "unverified")
    price_status = str(item.get("price_status") or "unknown")
    bucket = str(item.get("bucket") or "")
    related_stocks = [str(value).strip() for value in _as_list(item.get("related_stocks")) if str(value).strip()]

    if bucket == "filtered" or verification == "disproved":
        evidence = _gate("evidence", "fail", "信息已过滤或证伪，不能进入正向决策。")
    elif verification in {"verified", "cross_verified"}:
        complete = (
            _nonempty(item.get("marginal_change"))
            and bool(item.get("published_at"))
            and _traceable_information(item)
        )
        evidence = _gate(
            "evidence",
            "pass" if complete else "wait",
            "硬变化已验证且时间、来源可追溯。" if complete else "验证状态较高，但首次时间、原始来源或边际变化仍不完整。",
        )
    elif verification == "partial":
        evidence = _gate("evidence", "wait", "只有部分验证，仍缺独立或正式来源确认。")
    else:
        evidence = _gate("evidence", "missing", "当前仍是未验证线索。")

    industry = _gate(
        "industry",
        "wait" if item.get("promoted_chain_id") else "missing",
        "已经转入产业趋势，但是否形成独立产业确认仍需在产业页判断。"
        if item.get("promoted_chain_id")
        else "信息页没有产业链独立确认，不能单独证明方向。",
    )
    stock = _gate(
        "stock",
        "wait" if related_stocks else "missing",
        "已有股票映射，但尚未完成同链比较与利润传导确认。"
        if related_stocks
        else "尚无可比较的A股表达。",
        facts=related_stocks[:3],
    )
    pricing = _gate(
        "pricing",
        "wait" if _nonempty(item.get("price_summary")) else "missing",
        "信息页只有行情描述，尚缺市值、一致预期和隐含未来的同口径判断。"
        if _nonempty(item.get("price_summary"))
        else "定价信息缺失。",
    )
    if price_status == "first_expression":
        timing = _gate("timing", "pass", "信息后出现首次盘面表达；仍需产业与定价策略共同判断。")
    elif price_status in {"multi_rounds", "negative"}:
        timing = _gate("timing", "fail", "已经交易多轮或出现负反馈，时点不支持直接升级。")
    else:
        timing = _gate("timing", "wait", "盘面尚未确认，或当前行情口径不足。")

    qq_gates = [evidence, industry, stock, pricing, timing]
    qq_code = _qq_code(qq_gates)

    if bucket == "filtered" or verification == "disproved":
        baseline_code: DecisionCode = "D"
        baseline_reason = "简单基线只过滤已证伪或主动过滤的信息。"
    elif verification not in {"verified", "cross_verified"}:
        baseline_code = "C"
        baseline_reason = "简单基线把未充分验证的信息留在观察层。"
    elif price_status in {"untraded", "first_expression"} and related_stocks:
        baseline_code = "A"
        baseline_reason = "简单基线只看已验证、存在股票映射且尚未多轮交易。"
    else:
        baseline_code = "B"
        baseline_reason = "简单基线认为信息成立，但价格或股票表达暂不适合直接进入A类。"

    quality = _quality_checks_for_information(item)
    policies = [
        _policy(
            policy_id=QQ_POLICY_ID,
            version=QQ_POLICY_VERSION,
            label="qq 2.1 实验策略",
            recommendation=qq_code,
            reason=next((gate["summary"] for gate in qq_gates if gate["state"] != "pass"), "五项均满足当前qq口径。"),
            gates=qq_gates,
            scope="研究流程假设；尚未证明适用于全部信号类型",
        ),
        _policy(
            policy_id=BASELINE_POLICY_ID,
            version=BASELINE_POLICY_VERSION,
            label="简单证据/价格基线",
            recommendation=baseline_code,
            reason=baseline_reason,
            scope="对照组；复刻较少假设的旧式分层",
        ),
    ]
    return {
        "policies": policies,
        "disagreement": len({item["recommendation"] for item in policies}) > 1,
        "quality_checks": quality,
        "quality_complete": all(item["passed"] for item in quality),
        "principle": "策略建议只用于对照，不自动覆盖人工冻结判断。",
    }


def _hard_industry_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in sources:
        tier = str(row.get("source_tier") or "")
        verification = str(row.get("verification_status") or "")
        soft_tier = any(token in tier for token in ("雪球", "市场", "盘面", "媒体", "Codex", "推断"))
        hard_tier = any(token in tier for token in ("官方", "公司披露", "公司公告", "行业标准"))
        verified = verification in {"已互证", "单一来源", "已复核"} or any(
            token in verification for token in ("互证", "复核")
        )
        if (
            row.get("evidence_state") == "有效"
            and hard_tier
            and not soft_tier
            and verified
            and _nonempty(row.get("source_name") or row.get("source_url"))
        ):
            result.append(row)
    return result


def _independent_source_count(sources: list[dict[str, Any]]) -> int:
    keys = {
        str(row.get("source_name") or row.get("source_url") or "").strip().lower()
        for row in sources
        if str(row.get("source_name") or row.get("source_url") or "").strip()
    }
    return len(keys)


def _quality_checks_for_industry(
    chain: dict[str, Any],
    company: dict[str, Any] | None,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    checks = [
        ("time_frozen", "变化时间", bool(chain.get("last_change_at")), "缺少本轮变化时间"),
        ("change_explicit", "首次变化", _nonempty(chain.get("change_summary")), "没有写清本轮新增变化"),
        ("source_traceable", "产业来源", bool(sources), "尚无产业来源"),
        ("stock_selected", "主选公司", bool(company), "尚未选择主选公司"),
        (
            "invalidation_explicit",
            "证伪条件",
            _nonempty(chain.get("invalidation")),
            "缺少产业证伪条件",
        ),
    ]
    return [
        {"key": key, "label": label, "passed": passed, "message": "已记录" if passed else missing}
        for key, label, passed, missing in checks
    ]


def evaluate_industry_policies(
    chain: dict[str, Any],
    company: dict[str, Any] | None = None,
    *,
    why_best: str | None = None,
) -> dict[str, Any]:
    sources = [_as_dict(value) for value in _as_list(chain.get("sources"))]
    hard_sources = _hard_industry_sources(sources)
    independent_count = _independent_source_count(hard_sources)
    direction = str(chain.get("direction_verdict") or "观察")
    stock_verdict = str(chain.get("stock_verdict") or "观察")
    timing_verdict = str(chain.get("timing_verdict") or "观察")

    if direction == "否决":
        evidence = _gate("evidence", "fail", "产业方向已被当前研究否决。")
    elif hard_sources and _nonempty(chain.get("change_summary")):
        evidence = _gate(
            "evidence",
            "pass",
            f"存在{len(hard_sources)}条有效硬来源，并记录了本轮变化。",
            facts=[str(row.get("title") or row.get("source_name")) for row in hard_sources[:3]],
        )
    else:
        evidence = _gate("evidence", "wait", "缺少有效硬来源，或没有写清本轮新增变化。")

    if direction == "否决":
        industry = _gate("industry", "fail", "方向判断为否决。")
    elif direction == "通过" and independent_count >= 2:
        industry = _gate("industry", "pass", f"当前有{independent_count}个独立硬来源支持产业方向。")
    else:
        industry = _gate(
            "industry",
            "wait",
            f"当前独立硬来源为{independent_count}个；qq 2.1 的两个独立确认仍是待验证假设。",
        )

    if stock_verdict == "否决":
        stock = _gate("stock", "fail", "产业研究已否决当前股票表达。")
    elif not company:
        stock = _gate("stock", "missing", "尚未选择主选A股公司。")
    else:
        stock_complete = (
            company.get("verification_status") == "已确认"
            and company.get("benefit_directness") in {"强", "直接"}
            and _nonempty(company.get("profit_path"))
            and _nonempty(why_best or company.get("primary_reason") or company.get("company_standing"))
        )
        stock = _gate(
            "stock",
            "pass" if stock_complete else "wait",
            "主选已经确认直接利润传导，并给出同链比较依据。"
            if stock_complete
            else "主选仍缺已确认关系、直接利润路径或同链比较依据。",
            facts=[str(company.get("name") or "")],
        )

    if not company:
        pricing = _gate("pricing", "missing", "没有主选公司，无法判断个股定价。")
    else:
        gap_status = str(company.get("expectation_gap_status") or "无法判断")
        market_cap = company.get("expectation_anchor_market_cap")
        market_cap_snapshot = _as_dict(company.get("market_cap_snapshot"))
        pricing_complete = (
            gap_status != "无法判断"
            and _nonempty(company.get("market_implied_expectation"))
            and _nonempty(company.get("evidence_based_expectation"))
            and bool(company.get("expectation_as_of"))
            and (market_cap is not None or bool(market_cap_snapshot))
        )
        if gap_status == "负向预期差" or chain.get("pricing_status") == "充分定价":
            pricing = _gate("pricing", "fail", f"当前定价判断为{gap_status or chain.get('pricing_status')}。")
        elif gap_status == "正向预期差" and pricing_complete:
            pricing = _gate("pricing", "pass", "市值锚、市场隐含未来和硬证据支持未来已按同日口径记录。")
        else:
            pricing = _gate("pricing", "wait", "预期差仍无法精确判断，或缺市值日期/盈利锚/一致预期。")

    dynamics = _as_dict(chain.get("dynamics"))
    expression = _as_dict(dynamics.get("expression"))
    market_confirmed = bool(expression.get("market_confirmed"))
    if timing_verdict == "否决":
        timing = _gate("timing", "fail", "产业研究当前否决时点。")
    elif timing_verdict == "通过" and market_confirmed:
        timing = _gate("timing", "pass", "时点判断通过，主选盘面也出现有效表达。")
    else:
        timing = _gate("timing", "wait", "时点或主选盘面尚未同时确认。")

    qq_gates = [evidence, industry, stock, pricing, timing]
    qq_code = _qq_code(qq_gates)

    if "否决" in {direction, stock_verdict}:
        baseline_code: DecisionCode = "D"
    elif direction == "通过" and stock_verdict == "通过" and timing_verdict == "通过":
        baseline_code = "A" if chain.get("pricing_status") != "充分定价" else "B"
    elif direction == "通过" and stock_verdict == "通过":
        baseline_code = "B"
    else:
        baseline_code = "C"
    baseline_reason = (
        f"简单基线只使用产业页的方向/股票/时点结论："
        f"{direction}/{stock_verdict}/{timing_verdict}，定价为{chain.get('pricing_status') or '未知'}。"
    )

    quality = _quality_checks_for_industry(chain, company, sources)
    policies = [
        _policy(
            policy_id=QQ_POLICY_ID,
            version=QQ_POLICY_VERSION,
            label="qq 2.1 实验策略",
            recommendation=qq_code,
            reason=next((gate["summary"] for gate in qq_gates if gate["state"] != "pass"), "五项均满足当前qq口径。"),
            gates=qq_gates,
            scope="产业与个股研究假设；需按信号家族、市场和阶段分别验证",
        ),
        _policy(
            policy_id=BASELINE_POLICY_ID,
            version=BASELINE_POLICY_VERSION,
            label="简单三判断基线",
            recommendation=baseline_code,
            reason=baseline_reason,
            scope="对照组；不要求两源确认或完整市值反推",
        ),
    ]
    return {
        "policies": policies,
        "disagreement": len({item["recommendation"] for item in policies}) > 1,
        "quality_checks": quality,
        "quality_complete": all(item["passed"] for item in quality),
        "principle": "qq与简单基线都是待验证策略，不自动覆盖人工冻结判断。",
    }


def evaluation_for_frozen_decision(
    snapshot: dict[str, Any],
    *,
    source_kind: str,
    primary_stock_name: str | None,
    primary_full_code: str | None,
    why_best: str | None,
) -> dict[str, Any]:
    stored = _as_dict(snapshot.get("policy_evaluation"))
    if stored.get("policies"):
        return stored
    if source_kind == "industry_trend":
        chain = _as_dict(snapshot.get("industry"))
        companies = [_as_dict(value) for value in _as_list(chain.get("companies"))]
        company = next(
            (
                row
                for row in companies
                if (primary_full_code and row.get("full_code") == primary_full_code)
                or (primary_stock_name and row.get("name") == primary_stock_name)
            ),
            None,
        )
        return evaluate_industry_policies(chain, company, why_best=why_best)
    return evaluate_information_policies(_as_dict(snapshot.get("information")))


def attach_manual_comparison(evaluation: dict[str, Any], manual_code: DecisionCode) -> dict[str, Any]:
    result = {**evaluation, "manual_code": manual_code}
    result["manual_disagreements"] = [
        {
            "policy_id": policy["policy_id"],
            "policy_label": policy["label"],
            "policy_code": policy["recommendation"],
            "manual_code": manual_code,
        }
        for policy in _as_list(evaluation.get("policies"))
        if policy.get("recommendation") != manual_code
    ]
    result["manual_aligned_with_all"] = not result["manual_disagreements"]
    return result
