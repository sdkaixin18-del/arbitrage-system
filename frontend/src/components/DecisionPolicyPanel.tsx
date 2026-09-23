import { Alert, Tag, Typography } from "antd";
import type {
  DecisionCode,
  DecisionPolicyEvaluation,
  PolicyGateState
} from "../api/decisionReview";

const CODE_COLOR: Record<DecisionCode, string> = {
  A: "green",
  B: "gold",
  C: "blue",
  D: "default"
};

const GATE_META: Record<PolicyGateState, { color: string; label: string }> = {
  pass: { color: "green", label: "满足" },
  wait: { color: "gold", label: "待确认" },
  fail: { color: "red", label: "不满足" },
  missing: { color: "default", label: "缺失" }
};

export function policyConsensusDefault(evaluation?: DecisionPolicyEvaluation | null): DecisionCode {
  const values = evaluation?.policies.map((policy) => policy.recommendation) ?? [];
  if (values.length && values.every((value) => value === values[0])) return values[0];
  return "C";
}

export function DecisionPolicyInline({ evaluation }: { evaluation?: DecisionPolicyEvaluation | null }) {
  if (!evaluation?.policies.length) return <Tag>策略待计算</Tag>;
  return (
    <span className="decision-policy-inline">
      {evaluation.policies.map((policy) => (
        <Tag key={policy.policy_id} color={CODE_COLOR[policy.recommendation]}>
          {policy.policy_id === "qq_2_1" ? "qq实验" : "简单基线"} {policy.recommendation}
        </Tag>
      ))}
      {evaluation.disagreement ? <Tag color="volcano">有分歧</Tag> : <Tag color="green">同结论</Tag>}
    </span>
  );
}

export default function DecisionPolicyPanel({
  evaluation,
  compact = false
}: {
  evaluation?: DecisionPolicyEvaluation | null;
  compact?: boolean;
}) {
  if (!evaluation?.policies.length) {
    return <Alert showIcon type="warning" message="策略对照尚未生成" />;
  }
  const missingQuality = evaluation.quality_checks.filter((item) => !item.passed);
  return (
    <section className={`decision-policy-panel ${compact ? "is-compact" : ""}`}>
      <header>
        <div>
          <Typography.Text strong>策略对照，不是标准答案</Typography.Text>
          <Typography.Text type="secondary">{evaluation.principle}</Typography.Text>
        </div>
        {evaluation.disagreement ? <Tag color="volcano">策略结论分歧</Tag> : <Tag color="green">策略暂时同结论</Tag>}
      </header>
      <div className="decision-policy-grid">
        {evaluation.policies.map((policy) => (
          <article key={policy.policy_id}>
            <div className="decision-policy-title">
              <span>
                <strong>{policy.label}</strong>
                <small>{policy.version}</small>
              </span>
              <Tag color={CODE_COLOR[policy.recommendation]}>{policy.recommendation}</Tag>
            </div>
            <p>{policy.reason}</p>
            {!compact && policy.gates.length ? (
              <div className="decision-policy-gates">
                {policy.gates.map((gate) => (
                  <div key={gate.key} title={gate.summary}>
                    <span>{gate.label}</span>
                    <Tag color={GATE_META[gate.state].color}>{GATE_META[gate.state].label}</Tag>
                  </div>
                ))}
              </div>
            ) : null}
            {!compact ? <small className="decision-policy-scope">{policy.scope}</small> : null}
          </article>
        ))}
      </div>
      {missingQuality.length ? (
        <div className="decision-policy-quality">
          <strong>数据缺口</strong>
          {missingQuality.map((item) => <Tag key={item.key} color="orange">{item.label}：{item.message}</Tag>)}
        </div>
      ) : (
        <div className="decision-policy-quality"><Tag color="green">基础数据检查完整</Tag></div>
      )}
    </section>
  );
}
