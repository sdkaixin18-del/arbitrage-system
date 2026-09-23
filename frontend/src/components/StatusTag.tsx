import { Tag } from "antd";
import type { SourceStatus } from "../api";

export const statusColor: Record<string, string> = {
  loading: "processing",
  ok: "success",
  not_configured: "default",
  manual_only: "processing",
  local_quotes_only: "processing",
  needs_authorization: "warning",
  partial_error: "error",
  error: "error",
  not_found: "default",
  test: "processing",
  pending: "processing",
  pending_ai: "processing",
  skipped: "default"
};

export const statusLabel: Record<string, string> = {
  loading: "读取中",
  ok: "正常",
  not_configured: "未配置",
  manual_only: "手动处理",
  local_quotes_only: "本地缓存",
  needs_authorization: "需登录",
  partial_error: "部分异常",
  error: "异常",
  not_found: "暂无数据",
  test: "测试",
  pending: "待抓取",
  pending_ai: "待AI判断",
  skipped: "已跳过"
};

export default function StatusTag({ status }: { status?: SourceStatus | string | null }) {
  const value = status ?? "not_configured";
  return <Tag color={statusColor[value] ?? "default"}>{statusLabel[value] ?? value}</Tag>;
}
