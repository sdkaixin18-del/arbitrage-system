import {
  ArrowLeftOutlined,
  DeleteOutlined,
  EditOutlined,
  ExportOutlined,
  HistoryOutlined,
  PlusOutlined,
  SaveOutlined
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Input,
  List,
  Modal,
  Select,
  Space,
  Tabs,
  Tag,
  Typography,
  message
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useBlocker, useNavigate, useParams } from "react-router-dom";
import { api, type StockBar } from "../api";
import {
  stockResearchApi,
  type EvidenceCategory,
  type ResearchBundle,
  type ResearchEvidence,
  type ResearchEvidencePayload,
  type ResearchStatus,
  type ResearchWorkspacePayload
} from "../api/stockResearch";
import AppChart from "../components/AppChart";

const { TextArea } = Input;
const STATUS_OPTIONS: ResearchStatus[] = ["待研究", "跟踪中", "重点跟踪", "已验证", "已证伪", "暂不跟踪"];
const EVIDENCE_CATEGORIES: Array<{ key: EvidenceCategory; label: string; hint: string }> = [
  { key: "hard_fact", label: "硬事实", hint: "公告、订单、业绩、客户、认证、产能、政策等可核验事实" },
  { key: "xueqiu_clue", label: "雪球线索", hint: "雪球用户发言和跟踪线索，尚需外部确认" },
  { key: "market_hypothesis", label: "市场猜想", hint: "市场可能正在交易的叙事与假设" },
  { key: "tape_confirmation", label: "盘面确认", hint: "价格、成交、板块共振和相对强弱的确认" }
];

function emptyDraft(): ResearchWorkspacePayload {
  return {
    research_status: "待研究",
    current_conclusion: "",
    chart_expression: "",
    fundamental_change: "",
    market_trading: "",
    industry_position: "",
    risks_and_invalidation: "",
    next_validation: "",
    tag_names: []
  };
}

function bundleDraft(bundle: ResearchBundle): ResearchWorkspacePayload {
  const workspace = bundle.workspace;
  return workspace
    ? {
        research_status: workspace.research_status,
        current_conclusion: workspace.current_conclusion,
        chart_expression: workspace.chart_expression,
        fundamental_change: workspace.fundamental_change,
        market_trading: workspace.market_trading,
        industry_position: workspace.industry_position,
        risks_and_invalidation: workspace.risks_and_invalidation,
        next_validation: workspace.next_validation,
        tag_names: bundle.tag_names
      }
    : { ...emptyDraft(), tag_names: bundle.tag_names };
}

function formatTime(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

function formatPct(value: number | null) {
  if (value === null || Number.isNaN(value)) return "-";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function defaultEvidence(category: EvidenceCategory): ResearchEvidencePayload {
  return {
    category,
    confirmation_status: "待确认",
    evidence_date: new Date().toISOString().slice(0, 10),
    title: "",
    content: "",
    source_name: null,
    source_url: null
  };
}

function klineOption(rows: StockBar[]) {
  return {
    animation: false,
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    grid: { left: 54, right: 18, top: 20, bottom: 62 },
    xAxis: { type: "category", data: rows.map((row) => row.trade_date), boundaryGap: true },
    yAxis: { scale: true, splitLine: { lineStyle: { color: "#eef1ef" } } },
    dataZoom: [{ type: "inside", start: 45, end: 100 }, { type: "slider", start: 45, end: 100, bottom: 12, height: 22 }],
    series: [
      {
        type: "candlestick",
        data: rows.map((row) => [row.open, row.close, row.low, row.high]),
        itemStyle: { color: "#d84a3a", color0: "#16866f", borderColor: "#d84a3a", borderColor0: "#16866f" }
      }
    ]
  };
}

function CompactResearchField({
  label,
  placeholder,
  value,
  editing,
  onChange,
  conclusion = false
}: {
  label: string;
  placeholder: string;
  value: string;
  editing: boolean;
  onChange: (value: string) => void;
  conclusion?: boolean;
}) {
  return (
    <div className={`research-compact-field${conclusion ? " is-conclusion" : ""}`}>
      <Typography.Text strong>{label}</Typography.Text>
      {editing ? (
        <TextArea
          value={value}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value)}
          autoSize={{ minRows: conclusion ? 2 : 2, maxRows: conclusion ? 5 : 6 }}
        />
      ) : value ? (
        <Typography.Paragraph className="research-field-value">{value}</Typography.Paragraph>
      ) : (
        <Typography.Text type="secondary">未填写</Typography.Text>
      )}
    </div>
  );
}

export default function StockResearchPage() {
  const { fullCode = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ResearchWorkspacePayload>(emptyDraft);
  const [baseline, setBaseline] = useState(JSON.stringify(emptyDraft()));
  const [editing, setEditing] = useState(false);
  const [activeEvidence, setActiveEvidence] = useState<EvidenceCategory>("hard_fact");
  const [evidenceModal, setEvidenceModal] = useState<ResearchEvidence | "new" | null>(null);
  const [evidenceDraft, setEvidenceDraft] = useState<ResearchEvidencePayload>(defaultEvidence("hard_fact"));
  const [historyOpen, setHistoryOpen] = useState(false);
  const blockerConfirmOpen = useRef(false);

  const research = useQuery({ queryKey: ["stock-research", fullCode], queryFn: () => stockResearchApi.get(fullCode), enabled: Boolean(fullCode) });
  const bars = useQuery({ queryKey: ["stock-bars", fullCode, "6m"], queryFn: () => api.stockBars(fullCode, "6m"), enabled: Boolean(fullCode) });
  const factorTags = useQuery({ queryKey: ["factor-tags"], queryFn: api.factorTags, staleTime: 5 * 60_000 });
  const versions = useQuery({
    queryKey: ["stock-research-versions", fullCode],
    queryFn: () => stockResearchApi.versions(fullCode),
    enabled: Boolean(research.data?.workspace && historyOpen)
  });

  useEffect(() => {
    if (!research.data) return;
    const next = bundleDraft(research.data);
    setDraft(next);
    setBaseline(JSON.stringify(next));
    setEditing(!research.data.workspace);
  }, [research.data]);

  const dirty = JSON.stringify(draft) !== baseline;
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const blocker = useBlocker(dirty);
  useEffect(() => {
    if (blocker.state !== "blocked" || blockerConfirmOpen.current) return;
    blockerConfirmOpen.current = true;
    Modal.confirm({
      title: "有未保存修改",
      content: "离开后本次编辑会丢失。",
      okText: "放弃并离开",
      okButtonProps: { danger: true },
      cancelText: "继续编辑",
      onOk: () => blocker.proceed(),
      onCancel: () => blocker.reset(),
      afterClose: () => { blockerConfirmOpen.current = false; }
    });
  }, [blocker]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["stock-research", fullCode] });
    queryClient.invalidateQueries({ queryKey: ["stock-research-versions", fullCode] });
    queryClient.invalidateQueries({ queryKey: ["research-stocks"] });
    queryClient.invalidateQueries({ queryKey: ["factor-tags"] });
  };

  const save = useMutation({
    mutationFn: () => stockResearchApi.save(fullCode, draft),
    onSuccess: (bundle) => {
      const next = bundleDraft(bundle);
      queryClient.setQueryData(["stock-research", fullCode], bundle);
      setDraft(next);
      setBaseline(JSON.stringify(next));
      setEditing(false);
      invalidate();
      message.success(`已保存为第 ${bundle.workspace?.revision ?? 1} 版`);
    },
    onError: (error) => message.error(String(error))
  });

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (editing && dirty && !save.isPending) save.mutate();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [dirty, editing, save]);

  const evidenceMutation = useMutation({
    mutationFn: () =>
      evidenceModal === "new"
        ? stockResearchApi.createEvidence(fullCode, evidenceDraft)
        : stockResearchApi.updateEvidence(fullCode, (evidenceModal as ResearchEvidence).id, evidenceDraft),
    onSuccess: () => {
      message.success(evidenceModal === "new" ? "证据已新增" : "证据已更新");
      setEvidenceModal(null);
      invalidate();
    },
    onError: (error) => message.error(String(error))
  });

  const deleteEvidence = useMutation({
    mutationFn: (id: number) => stockResearchApi.deleteEvidence(fullCode, id),
    onSuccess: () => { message.success("证据已删除"); invalidate(); },
    onError: (error) => message.error(String(error))
  });

  const restore = useMutation({
    mutationFn: (versionId: number) => stockResearchApi.restore(fullCode, versionId),
    onSuccess: (bundle) => {
      queryClient.setQueryData(["stock-research", fullCode], bundle);
      setHistoryOpen(false);
      invalidate();
      message.success("历史版本已恢复，并生成了新的保存版本");
    },
    onError: (error) => message.error(String(error))
  });

  const chart = useMemo(() => klineOption(bars.data ?? []), [bars.data]);
  const stock = research.data?.stock;
  const visibleEvidence = (research.data?.evidence ?? []).filter((item) => item.category === activeEvidence);
  const pctColor = (stock?.change_pct ?? 0) > 0 ? "#c2410c" : (stock?.change_pct ?? 0) < 0 ? "#16866f" : undefined;

  const update = <K extends keyof ResearchWorkspacePayload>(key: K, value: ResearchWorkspacePayload[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const openEvidence = (item?: ResearchEvidence) => {
    if (!research.data?.workspace) {
      message.info("请先保存个股投研，再添加证据");
      return;
    }
    setEvidenceDraft(item ? {
      category: item.category,
      confirmation_status: item.confirmation_status,
      evidence_date: item.evidence_date,
      title: item.title,
      content: item.content,
      source_name: item.source_name,
      source_url: item.source_url
    } : defaultEvidence(activeEvidence));
    setEvidenceModal(item ?? "new");
  };

  if (research.isError) {
    return <main className="page"><Alert type="error" showIcon message="个股投研读取失败" description={String(research.error)} /></main>;
  }

  return (
    <main className="page stock-research-page">
      <header className="stock-research-header panel">
        <div className="stock-research-title">
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate("/research")} />
          <div>
            <Space align="baseline" wrap>
              <Typography.Title level={2}>{stock?.name ?? "加载中"}</Typography.Title>
              <Typography.Text type="secondary">{stock?.full_code}</Typography.Text>
            </Space>
            <Space wrap>
              <strong className="stock-research-price">{stock?.latest_price?.toFixed(2) ?? "-"}</strong>
              <strong style={{ color: pctColor }}>{formatPct(stock?.change_pct ?? null)}</strong>
              <Typography.Text type="secondary">行情 {stock?.quote_date ?? "-"} · 更新 {formatTime(stock?.quote_updated_at)}</Typography.Text>
              {research.data?.workspace ? <Tag>第 {research.data.workspace.revision} 版</Tag> : <Tag>尚未保存</Tag>}
              {dirty ? <Tag color="orange">有未保存修改</Tag> : null}
            </Space>
          </div>
        </div>
        <Space wrap>
          <Select
            value={draft.research_status}
            options={STATUS_OPTIONS.map((status) => ({ label: status, value: status }))}
            disabled={!editing}
            onChange={(value) => update("research_status", value)}
          />
          <Button icon={<HistoryOutlined />} disabled={!research.data?.workspace} onClick={() => setHistoryOpen(true)}>历史</Button>
          {editing ? (
            <>
              {research.data?.workspace ? <Button onClick={() => { const next = bundleDraft(research.data as ResearchBundle); setDraft(next); setBaseline(JSON.stringify(next)); setEditing(false); }}>取消</Button> : null}
              <Button type="primary" icon={<SaveOutlined />} loading={save.isPending} disabled={!dirty && Boolean(research.data?.workspace)} onClick={() => save.mutate()}>
                保存 <span className="shortcut-hint">⌘S</span>
              </Button>
            </>
          ) : (
            <Button type="primary" icon={<EditOutlined />} onClick={() => setEditing(true)}>编辑</Button>
          )}
        </Space>
      </header>

      <div className="stock-research-layout">
        <div className="stock-research-main">
          <section className="panel research-core-card">
            <CompactResearchField
              label="当前结论"
              placeholder="一句话写清当前判断"
              value={draft.current_conclusion}
              editing={editing}
              conclusion
              onChange={(value) => update("current_conclusion", value)}
            />
          </section>

          <section className="panel research-core-card">
            <Typography.Title level={4}>盘面表达</Typography.Title>
            <div className="research-field-grid">
              <CompactResearchField
                label="图形表达"
                placeholder="首次、加速、分歧、反包或趋势延续"
                value={draft.chart_expression}
                editing={editing}
                onChange={(value) => update("chart_expression", value)}
              />
              <CompactResearchField
                label="市场交易逻辑"
                placeholder="当前股价映射的核心变量"
                value={draft.market_trading}
                editing={editing}
                onChange={(value) => update("market_trading", value)}
              />
            </div>
          </section>

          <section className="panel research-core-card">
            <Typography.Title level={4}>基本面与产业链</Typography.Title>
            <div className="research-field-grid">
              <CompactResearchField
                label="基本面变化"
                placeholder="订单、涨价、业绩、客户、认证或产能变化"
                value={draft.fundamental_change}
                editing={editing}
                onChange={(value) => update("fundamental_change", value)}
              />
              <CompactResearchField
                label="产业链位置"
                placeholder="所处环节、受益直接性与竞争格局"
                value={draft.industry_position}
                editing={editing}
                onChange={(value) => update("industry_position", value)}
              />
            </div>
          </section>

          <Collapse
            className="panel research-advanced-collapse"
            bordered={false}
            items={[
              {
                key: "risk-next",
                label: <Typography.Text strong>风险与下一步</Typography.Text>,
                children: (
                  <div className="research-field-grid">
                    <CompactResearchField
                      label="风险与证伪"
                      placeholder="什么情况出现后，当前判断不再成立"
                      value={draft.risks_and_invalidation}
                      editing={editing}
                      onChange={(value) => update("risks_and_invalidation", value)}
                    />
                    <CompactResearchField
                      label="下一步验证"
                      placeholder="下一条硬证据、时间节点或跟踪动作"
                      value={draft.next_validation}
                      editing={editing}
                      onChange={(value) => update("next_validation", value)}
                    />
                  </div>
                )
              }
            ]}
          />
        </div>

        <aside className="stock-research-side">
          <section className="panel stock-kline-card">
            <div className="section-title-row"><Typography.Title level={4}>日 K</Typography.Title><Typography.Text type="secondary">近 6 个月</Typography.Text></div>
            {bars.data?.length ? <AppChart option={chart} className="stock-research-kline" /> : <Empty description={bars.isLoading ? "K 线加载中" : "暂无 K 线"} />}
          </section>
          <section className="panel stock-meta-card">
            <Typography.Title level={4}>因子标签</Typography.Title>
            {editing ? (
              <Select
                mode="tags"
                style={{ width: "100%" }}
                value={draft.tag_names}
                options={(factorTags.data ?? []).map((tag) => ({ label: tag.name, value: tag.name }))}
                onChange={(value) => update("tag_names", Array.from(new Set(value.map((item) => item.trim()).filter(Boolean))))}
                placeholder="选择或直接输入标签"
              />
            ) : draft.tag_names.length ? (
              <Space wrap>{draft.tag_names.map((tag) => <Tag key={tag}>{tag}</Tag>)}</Space>
            ) : <Typography.Text type="secondary">未关联标签</Typography.Text>}
          </section>
          <section className="panel stock-meta-card">
            <Typography.Title level={4}>板块 / 产业链</Typography.Title>
            <List
              size="small"
              dataSource={research.data?.relations ?? []}
              locale={{ emptyText: "暂无关联" }}
              renderItem={(relation) => (
                <List.Item>
                  <List.Item.Meta title={relation.link ? <Link to={relation.link}>{relation.name}</Link> : relation.name} description={`${relation.type}${relation.detail ? ` · ${relation.detail}` : ""}`} />
                </List.Item>
              )}
            />
          </section>
          <section className="panel stock-meta-card">
            <Typography.Title level={4}>外部链接</Typography.Title>
            <Space direction="vertical">
              <a href={`https://xueqiu.com/S/${stock?.full_code ?? fullCode}`} target="_blank" rel="noreferrer">雪球 <ExportOutlined /></a>
              <a href={`https://quote.eastmoney.com/${(stock?.full_code ?? fullCode).toLowerCase()}.html`} target="_blank" rel="noreferrer">东方财富 <ExportOutlined /></a>
            </Space>
          </section>
        </aside>
      </div>

      <section className="panel research-evidence-ledger">
        <div className="section-title-row">
          <div><Typography.Title level={3}>证据台账</Typography.Title><Typography.Text type="secondary">四类证据分开记录，避免把线索直接当结论。</Typography.Text></div>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openEvidence()}>新增证据</Button>
        </div>
        <Tabs
          activeKey={activeEvidence}
          onChange={(key) => setActiveEvidence(key as EvidenceCategory)}
          items={EVIDENCE_CATEGORIES.map((category) => ({
            key: category.key,
            label: `${category.label} ${(research.data?.evidence ?? []).filter((item) => item.category === category.key).length}`,
            children: (
              <div>
                <Typography.Paragraph type="secondary">{category.hint}</Typography.Paragraph>
                <List
                  dataSource={visibleEvidence}
                  locale={{ emptyText: <Empty description={`暂无${category.label}`} /> }}
                  renderItem={(item) => (
                    <List.Item
                      className="research-evidence-row"
                      actions={[
                        <Button key="edit" type="text" icon={<EditOutlined />} onClick={() => openEvidence(item)}>编辑</Button>,
                        <Button key="delete" type="text" danger icon={<DeleteOutlined />} onClick={() => Modal.confirm({
                          title: "删除这条证据？", okText: "删除", okButtonProps: { danger: true }, cancelText: "取消", onOk: () => deleteEvidence.mutateAsync(item.id)
                        })}>删除</Button>
                      ]}
                    >
                      <List.Item.Meta
                        title={<Space wrap><strong>{item.title}</strong><Tag>{item.confirmation_status}</Tag><Typography.Text type="secondary">{item.evidence_date ?? "未填日期"}</Typography.Text></Space>}
                        description={<><Typography.Paragraph>{item.content || "未填写正文"}</Typography.Paragraph>{item.source_name || item.source_url ? <Typography.Text type="secondary">来源：{item.source_name || "链接"}{item.source_url ? <> · <a href={item.source_url} target="_blank" rel="noreferrer">查看原文</a></> : null}</Typography.Text> : null}</>}
                      />
                    </List.Item>
                  )}
                />
              </div>
            )
          }))}
        />
      </section>

      <Modal
        open={Boolean(evidenceModal)}
        title={evidenceModal === "new" ? "新增证据" : "编辑证据"}
        okText="保存"
        cancelText="取消"
        width={680}
        confirmLoading={evidenceMutation.isPending}
        okButtonProps={{ disabled: !evidenceDraft.title.trim() }}
        onOk={() => evidenceMutation.mutate()}
        onCancel={() => setEvidenceModal(null)}
      >
        <div className="evidence-form">
          <label>分类<Select value={evidenceDraft.category} options={EVIDENCE_CATEGORIES.map((item) => ({ label: item.label, value: item.key }))} onChange={(value) => setEvidenceDraft((current) => ({ ...current, category: value }))} /></label>
          <label>确认状态<Select value={evidenceDraft.confirmation_status} options={["待确认", "已确认", "部分确认", "已证伪"].map((value) => ({ label: value, value }))} onChange={(value) => setEvidenceDraft((current) => ({ ...current, confirmation_status: value }))} /></label>
          <label>日期<Input type="date" value={evidenceDraft.evidence_date ?? ""} onChange={(event) => setEvidenceDraft((current) => ({ ...current, evidence_date: event.target.value || null }))} /></label>
          <label className="is-wide">标题<Input value={evidenceDraft.title} onChange={(event) => setEvidenceDraft((current) => ({ ...current, title: event.target.value }))} /></label>
          <label className="is-wide">正文<TextArea value={evidenceDraft.content} autoSize={{ minRows: 4, maxRows: 10 }} onChange={(event) => setEvidenceDraft((current) => ({ ...current, content: event.target.value }))} /></label>
          <label>来源<Input value={evidenceDraft.source_name ?? ""} onChange={(event) => setEvidenceDraft((current) => ({ ...current, source_name: event.target.value || null }))} /></label>
          <label>链接<Input value={evidenceDraft.source_url ?? ""} onChange={(event) => setEvidenceDraft((current) => ({ ...current, source_url: event.target.value || null }))} /></label>
        </div>
      </Modal>

      <Drawer open={historyOpen} title="保存历史" width={520} onClose={() => setHistoryOpen(false)}>
        <List
          loading={versions.isLoading}
          dataSource={versions.data?.items ?? []}
          locale={{ emptyText: "暂无保存历史" }}
          renderItem={(version) => (
            <List.Item actions={[<Button key="restore" type="link" onClick={() => Modal.confirm({ title: `恢复第 ${version.revision} 版？`, content: "当前内容会被该版本覆盖，并自动生成一个新的保存版本。", okText: "恢复", cancelText: "取消", onOk: () => restore.mutateAsync(version.id) })}>恢复</Button>] }>
              <Descriptions size="small" column={1} title={`第 ${version.revision} 版`}>
                <Descriptions.Item label="保存时间">{formatTime(version.created_at)}</Descriptions.Item>
                <Descriptions.Item label="状态">{version.snapshot.workspace.research_status}</Descriptions.Item>
                <Descriptions.Item label="结论">{version.snapshot.workspace.current_conclusion || "未填写"}</Descriptions.Item>
              </Descriptions>
            </List.Item>
          )}
        />
      </Drawer>
    </main>
  );
}
