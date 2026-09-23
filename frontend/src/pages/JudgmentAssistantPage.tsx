import { ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Input, Select, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import {
  api,
  type JudgmentAssistantCard,
  type JudgmentConclusion,
  type JudgmentMotherCard,
  type JudgmentRecord,
  type JudgmentRecordPayload
} from "../api";

const { TextArea } = Input;

const beijingDateFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false
});

const dateText = (value?: string | null) => {
  if (!value) return "-";
  const normalized = /([zZ]|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return beijingDateFormatter.format(date).replace(/\//g, "-");
};

const conclusionColor = (value?: string | null) => {
  if (value === "通过") return "success";
  if (value === "观察") return "processing";
  if (value === "降级") return "warning";
  if (value === "放弃") return "error";
  return "default";
};

const motherLabel = (card: JudgmentMotherCard) => `${card.id}｜${card.title}`;
const originalLabel = (card: JudgmentAssistantCard) => `${card.id}｜${card.title}`;

type JudgmentRecordFormState = {
  judgment_object: string;
  called_cards: string[];
  satisfied: string;
  unsatisfied: string;
  conclusion: JudgmentConclusion;
  next_validation: string;
  note: string;
};

const defaultRecordForm = (selectedId?: string | null): JudgmentRecordFormState => ({
  judgment_object: "",
  called_cards: selectedId ? [selectedId] : [],
  satisfied: "",
  unsatisfied: "",
  conclusion: "观察",
  next_validation: "",
  note: ""
});

export default function JudgmentAssistantPage() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [recordForm, setRecordForm] = useState<JudgmentRecordFormState>(() => defaultRecordForm(null));
  const overview = useQuery({ queryKey: ["judgment-assistant"], queryFn: api.judgmentAssistant });

  const motherCards = overview.data?.mother_cards ?? [];
  const originalCards = overview.data?.cards ?? [];
  const selectedMother = motherCards.find((card) => card.id === selectedId) ?? null;
  const selectedOriginal = selectedMother ? null : originalCards.find((card) => card.id === selectedId) ?? null;
  const selectedType = selectedMother ? "mother" : selectedOriginal ? "original" : null;
  const selectedTitle = selectedMother?.title ?? selectedOriginal?.title ?? "判断清单";
  const supportingCards = selectedMother
    ? selectedMother.supporting_card_ids.map((id) => originalCards.find((card) => card.id === id)).filter(Boolean) as JudgmentAssistantCard[]
    : [];
  const parentMotherCards = selectedOriginal
    ? selectedOriginal.mother_card_ids.map((id) => motherCards.find((card) => card.id === id)).filter(Boolean) as JudgmentMotherCard[]
    : [];

  const callCardOptions = useMemo(
    () => [
      ...motherCards.map((card) => ({ label: `母卡｜${motherLabel(card)}`, value: card.id })),
      ...originalCards.map((card) => ({ label: `原始卡片｜${originalLabel(card)}`, value: card.id }))
    ],
    [motherCards, originalCards]
  );

  useEffect(() => {
    if (!selectedId && motherCards.length) {
      setSelectedId(motherCards[0].id);
      return;
    }
    if (!selectedId && originalCards.length) {
      setSelectedId(originalCards[0].id);
    }
  }, [motherCards, originalCards, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    setRecordForm((current) => (current.called_cards.length ? current : { ...current, called_cards: [selectedId] }));
  }, [selectedId]);

  const selectCard = (id: string) => {
    setSelectedId(id);
    setRecordForm((current) => ({ ...current, called_cards: [id] }));
  };

  const setRecordField = <Key extends keyof JudgmentRecordFormState>(field: Key, value: JudgmentRecordFormState[Key]) => {
    setRecordForm((current) => ({ ...current, [field]: value }));
  };

  const createRecord = useMutation({
    mutationFn: api.createJudgmentRecord,
    onSuccess: () => {
      message.success("判断记录已写入知识库");
      queryClient.invalidateQueries({ queryKey: ["judgment-assistant"] });
      setRecordForm(defaultRecordForm(selectedId));
    },
    onError: (error) => {
      message.error(error instanceof Error ? error.message : "保存失败");
    }
  });

  const recordColumns: ColumnsType<JudgmentRecord> = [
    { title: "时间", dataIndex: "date", width: 150 },
    { title: "判断对象", dataIndex: "judgment_object", width: 180, ellipsis: true },
    {
      title: "结论",
      dataIndex: "conclusion",
      width: 90,
      render: (value) => <Tag color={conclusionColor(value)}>{value || "-"}</Tag>
    },
    {
      title: "调用卡片",
      dataIndex: "called_cards",
      width: 300,
      render: (values: string[]) => values?.slice(0, 3).map((value) => <Tag key={value}>{value}</Tag>) || "-"
    },
    { title: "满足条件", dataIndex: "satisfied", ellipsis: true },
    { title: "不满足条件", dataIndex: "unsatisfied", ellipsis: true },
    { title: "后续验证", dataIndex: "next_validation", ellipsis: true }
  ];

  const submitRecord = () => {
    const payload: JudgmentRecordPayload = {
      ...recordForm,
      judgment_object: recordForm.judgment_object.trim(),
      satisfied: recordForm.satisfied.trim(),
      unsatisfied: recordForm.unsatisfied.trim(),
      next_validation: recordForm.next_validation.trim(),
      note: recordForm.note.trim(),
      called_cards: recordForm.called_cards
    };
    if (!payload.judgment_object) {
      message.warning("请输入判断对象");
      return;
    }
    if (!payload.called_cards.length) {
      message.warning("至少选择一张卡片");
      return;
    }
    if (!payload.conclusion) {
      message.warning("请选择当前结论");
      return;
    }
    createRecord.mutate(payload);
  };

  return (
    <main className="page judgment-page">
      <div className="page-header">
        <div>
          <Typography.Title level={2}>判断辅助</Typography.Title>
          <Typography.Text type="secondary">选择判断卡，记录证据与结论。</Typography.Text>
        </div>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => overview.refetch()}
          loading={overview.isFetching}
        >
          刷新
        </Button>
      </div>

      {overview.isError ? <Alert type="error" showIcon message="判断辅助接口暂时不可用" description={String(overview.error)} /> : null}
      {overview.data && overview.data.status !== "ok" ? (
        <Alert type="warning" showIcon message={overview.data.message} description={overview.data.library_path} />
      ) : null}

      <div className="panel judgment-source-panel">
        <Space size="middle" wrap>
          <Tag color={overview.data?.status === "ok" ? "success" : "warning"}>{overview.data?.status ?? "loading"}</Tag>
          <Typography.Text type="secondary">更新时间：{dateText(overview.data?.updated_at)}</Typography.Text>
          <Typography.Text type="secondary">判断卡 {motherCards.length + originalCards.length} 张</Typography.Text>
        </Space>
      </div>

      <div className="judgment-layout">
        <section className="panel judgment-card-panel">
          <div className="section-title-row">
            <Typography.Title level={4}>判断体系</Typography.Title>
          </div>

          <Typography.Text className="judgment-card-section-title" type="secondary">母卡</Typography.Text>
          {motherCards.length ? (
            <div className="judgment-card-list">
              {motherCards.map((card) => (
                <button
                  key={card.id}
                  type="button"
                  className={`judgment-card-item mother ${selectedId === card.id ? "active" : ""}`}
                  onClick={() => selectCard(card.id)}
                >
                  <span className="judgment-card-id">{card.id}</span>
                  <span className="judgment-card-title">{card.title}</span>
                </button>
              ))}
            </div>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有母卡" />
          )}

          <Typography.Text className="judgment-card-section-title" type="secondary">原始卡片 / 案例卡</Typography.Text>
          {originalCards.length ? (
            <div className="judgment-card-list">
              {originalCards.map((card) => (
                <button
                  key={card.id}
                  type="button"
                  className={`judgment-card-item ${selectedId === card.id ? "active" : ""}`}
                  onClick={() => selectCard(card.id)}
                >
                  <span className="judgment-card-id">{card.id}</span>
                  <span className="judgment-card-title">{card.title}</span>
                </button>
              ))}
            </div>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有原始卡片" />
          )}
        </section>

        <section className="panel judgment-detail-panel">
          <div className="section-title-row">
            <div>
              <Typography.Title level={4}>{selectedTitle}</Typography.Title>
              <Typography.Text type="secondary">
                {selectedType === "mother" ? `母卡 ${selectedMother?.id}` : selectedType === "original" ? `原始卡片 ${selectedOriginal?.id}` : "选择一张卡片后查看清单"}
              </Typography.Text>
            </div>
          </div>

          {selectedMother ? (
            <div className="judgment-detail-content">
              <div className="judgment-core">{selectedMother.problem || "这张母卡暂时没有解决问题描述。"}</div>

              <Typography.Title level={5}>调用条件</Typography.Title>
              {selectedMother.call_conditions.length ? (
                <ul className="judgment-boundary-list">
                  {selectedMother.call_conditions.map((item) => <li key={item}>{item}</li>)}
                </ul>
              ) : (
                <Typography.Text type="secondary">暂无</Typography.Text>
              )}

              <Typography.Title level={5}>核心判断清单</Typography.Title>
              {selectedMother.checklist.length ? (
                <ol className="judgment-checklist">
                  {selectedMother.checklist.map((item) => <li key={item}>{item}</li>)}
                </ol>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这张母卡还没有判断清单" />
              )}

              <Typography.Title level={5}>支持卡片</Typography.Title>
              {supportingCards.length ? (
                <div className="judgment-support-list">
                  {supportingCards.map((card) => (
                    <button key={card.id} type="button" className="judgment-support-item" onClick={() => selectCard(card.id)}>
                      <Tag>{card.id}</Tag>
                      <span>{card.title}</span>
                    </button>
                  ))}
                </div>
              ) : (
                <Typography.Text type="secondary">暂无</Typography.Text>
              )}

              <Typography.Title level={5}>适用边界</Typography.Title>
              {selectedMother.boundaries.length ? (
                <ul className="judgment-boundary-list">
                  {selectedMother.boundaries.map((item) => <li key={item}>{item}</li>)}
                </ul>
              ) : (
                <Typography.Text type="secondary">暂无</Typography.Text>
              )}

              <Typography.Title level={5}>修正记录</Typography.Title>
              <div className="judgment-note-list">
                {selectedMother.revision_notes.length ? selectedMother.revision_notes.map((item) => <Typography.Paragraph key={item}>{item}</Typography.Paragraph>) : <Typography.Text type="secondary">暂无</Typography.Text>}
              </div>

              <Typography.Title level={5}>后续复用提示</Typography.Title>
              <div className="judgment-core">{selectedMother.reuse_hint || "暂无"}</div>
            </div>
          ) : selectedOriginal ? (
            <div className="judgment-detail-content">
              <div className="judgment-core">{selectedOriginal.core || "这张卡片暂时没有一句话核心。"}</div>

              <Typography.Title level={5}>所属母卡</Typography.Title>
              {parentMotherCards.length ? (
                <div className="judgment-support-list">
                  {parentMotherCards.map((card) => (
                    <button key={card.id} type="button" className="judgment-support-item" onClick={() => selectCard(card.id)}>
                      <Tag color="processing">{card.id}</Tag>
                      <span>{card.title}</span>
                    </button>
                  ))}
                </div>
              ) : (
                <Typography.Text type="secondary">暂未归属母卡</Typography.Text>
              )}

              <Typography.Title level={5}>可执行判断清单</Typography.Title>
              {selectedOriginal.checklist.length ? (
                <ol className="judgment-checklist">
                  {selectedOriginal.checklist.map((item) => <li key={item}>{item}</li>)}
                </ol>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这张卡片还没有判断清单" />
              )}

              <Typography.Title level={5}>适用边界</Typography.Title>
              {selectedOriginal.boundaries.length ? (
                <ul className="judgment-boundary-list">
                  {selectedOriginal.boundaries.map((item) => <li key={item}>{item}</li>)}
                </ul>
              ) : (
                <Typography.Text type="secondary">暂无</Typography.Text>
              )}

              <Typography.Title level={5}>修正记录</Typography.Title>
              <div className="judgment-note-list">
                {selectedOriginal.revision_notes.length ? selectedOriginal.revision_notes.map((item) => <Typography.Paragraph key={item}>{item}</Typography.Paragraph>) : <Typography.Text type="secondary">暂无</Typography.Text>}
              </div>

              <Typography.Title level={5}>后续复用提示</Typography.Title>
              <div className="judgment-core">{selectedOriginal.reuse_hint || "暂无"}</div>
            </div>
          ) : (
            <Empty description="没有可用卡片" />
          )}
        </section>

        <section className="panel judgment-form-panel">
          <div className="section-title-row">
            <Typography.Title level={4}>记录一次判断</Typography.Title>
          </div>
          <div className="judgment-record-form">
            <label className="judgment-record-field">
              <span>判断对象</span>
              <Input value={recordForm.judgment_object} onChange={(event) => setRecordField("judgment_object", event.target.value)} placeholder="行业 / 公司 / 事件 / 线索" />
            </label>
            <label className="judgment-record-field">
              <span>调用卡片</span>
              <Select mode="multiple" value={recordForm.called_cards} onChange={(value) => setRecordField("called_cards", value)} options={callCardOptions} placeholder="优先选择母卡，再补充原始卡片" />
            </label>
            <label className="judgment-record-field">
              <span>满足条件</span>
              <TextArea value={recordForm.satisfied} onChange={(event) => setRecordField("satisfied", event.target.value)} rows={3} placeholder="哪些判断清单已经通过？" />
            </label>
            <label className="judgment-record-field">
              <span>不满足条件</span>
              <TextArea value={recordForm.unsatisfied} onChange={(event) => setRecordField("unsatisfied", event.target.value)} rows={3} placeholder="哪些地方证据不足、互相冲突，或需要降级？" />
            </label>
            <label className="judgment-record-field">
              <span>当前结论</span>
              <Select<JudgmentConclusion>
                value={recordForm.conclusion}
                onChange={(value) => setRecordField("conclusion", value)}
                options={[
                  { label: "通过", value: "通过" },
                  { label: "观察", value: "观察" },
                  { label: "降级", value: "降级" },
                  { label: "放弃", value: "放弃" }
                ]}
              />
            </label>
            <label className="judgment-record-field">
              <span>后续验证点</span>
              <TextArea value={recordForm.next_validation} onChange={(event) => setRecordField("next_validation", event.target.value)} rows={2} placeholder="接下来等什么数据、公告、财报或市场反馈？" />
            </label>
            <label className="judgment-record-field">
              <span>备注</span>
              <TextArea value={recordForm.note} onChange={(event) => setRecordField("note", event.target.value)} rows={2} placeholder="其他需要保留的判断背景" />
            </label>
            <Button type="primary" onClick={submitRecord} icon={<SaveOutlined />} loading={createRecord.isPending} block>
              写入判断记录
            </Button>
          </div>
        </section>
      </div>

      <section className="panel">
        <div className="section-title-row">
          <Typography.Title level={4}>最近判断记录</Typography.Title>
        </div>
        <Table
          rowKey="id"
          columns={recordColumns}
          dataSource={overview.data?.recent_records ?? []}
          pagination={{ pageSize: 8 }}
          loading={overview.isLoading}
        />
      </section>
    </main>
  );
}
