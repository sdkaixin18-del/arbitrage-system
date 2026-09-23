import { request } from "../apiClient";

export type ResearchStatus = "待研究" | "跟踪中" | "重点跟踪" | "已验证" | "已证伪" | "暂不跟踪";
export type EvidenceCategory = "hard_fact" | "xueqiu_clue" | "market_hypothesis" | "tape_confirmation";
export type EvidenceStatus = "待确认" | "已确认" | "部分确认" | "已证伪";

export interface ResearchStockSearchItem {
  code: string;
  name: string;
  exchange: string;
  full_code: string;
  pinyin?: string;
  score?: number;
}

export interface ResearchStockHeader extends ResearchStockSearchItem {
  latest_price: number | null;
  change_pct: number | null;
  quote_date: string | null;
  quote_updated_at: string | null;
}

export interface ResearchWorkspace {
  id: number;
  research_status: ResearchStatus;
  current_conclusion: string;
  chart_expression: string;
  fundamental_change: string;
  market_trading: string;
  industry_position: string;
  risks_and_invalidation: string;
  next_validation: string;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface ResearchRelation {
  id: number | null;
  name: string;
  type: string;
  detail: string | null;
  link: string | null;
}

export interface ResearchEvidence {
  id: number;
  category: EvidenceCategory;
  confirmation_status: EvidenceStatus;
  evidence_date: string | null;
  title: string;
  content: string;
  source_name: string | null;
  source_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface ResearchBundle {
  stock: ResearchStockHeader;
  workspace: ResearchWorkspace | null;
  tag_names: string[];
  relations: ResearchRelation[];
  evidence: ResearchEvidence[];
}

export interface ResearchWorkspacePayload {
  research_status: ResearchStatus;
  current_conclusion: string;
  chart_expression: string;
  fundamental_change: string;
  market_trading: string;
  industry_position: string;
  risks_and_invalidation: string;
  next_validation: string;
  tag_names: string[];
}

export interface ResearchEvidencePayload {
  category: EvidenceCategory;
  confirmation_status: EvidenceStatus;
  evidence_date: string | null;
  title: string;
  content: string;
  source_name: string | null;
  source_url: string | null;
}

export interface ResearchVersion {
  id: number;
  revision: number;
  created_at: string;
  snapshot: {
    workspace: Omit<ResearchWorkspacePayload, "tag_names">;
    tag_names: string[];
    evidence: ResearchEvidence[];
  };
}

export interface RecentResearchItem extends ResearchStockSearchItem {
  research_status: ResearchStatus;
  current_conclusion: string;
  revision: number;
  updated_at: string;
  tag_names: string[];
}

export const stockResearchApi = {
  searchStocks: (keyword: string, limit = 20) =>
    request<ResearchStockSearchItem[]>(`/api/stocks/search?keyword=${encodeURIComponent(keyword)}&limit=${limit}`),
  recentStocks: (query = "", limit = 40) =>
    request<{ items: RecentResearchItem[] }>(`/api/research/stocks?query=${encodeURIComponent(query)}&limit=${limit}`),
  get: (fullCode: string) => request<ResearchBundle>(`/api/stocks/${encodeURIComponent(fullCode)}/research`),
  save: (fullCode: string, payload: ResearchWorkspacePayload) =>
    request<ResearchBundle>(`/api/stocks/${encodeURIComponent(fullCode)}/research`, {
      method: "PUT",
      body: JSON.stringify(payload)
    }),
  createEvidence: (fullCode: string, payload: ResearchEvidencePayload) =>
    request<ResearchEvidence>(`/api/stocks/${encodeURIComponent(fullCode)}/research/evidence`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateEvidence: (fullCode: string, evidenceId: number, payload: Partial<ResearchEvidencePayload>) =>
    request<ResearchEvidence>(`/api/stocks/${encodeURIComponent(fullCode)}/research/evidence/${evidenceId}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteEvidence: (fullCode: string, evidenceId: number) =>
    request<{ status: string; message: string }>(
      `/api/stocks/${encodeURIComponent(fullCode)}/research/evidence/${evidenceId}`,
      { method: "DELETE" }
    ),
  versions: (fullCode: string) =>
    request<{ items: ResearchVersion[] }>(`/api/stocks/${encodeURIComponent(fullCode)}/research/versions`),
  restore: (fullCode: string, versionId: number) =>
    request<ResearchBundle>(`/api/stocks/${encodeURIComponent(fullCode)}/research/versions/${versionId}/restore`, {
      method: "POST"
    })
};
