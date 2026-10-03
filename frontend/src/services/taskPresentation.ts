import type { TaskStatus } from "./api";
export const statusLabels: Record<TaskStatus, string> = {
  pending: "排队中",
  processing: "执行中",
  completed: "已完成",
  partial_success: "需处理",
  failed: "失败",
  cancelled: "已取消",
};
export const stageLabels: Record<string, string> = {
  queued: "排队",
  converting: "文档转换",
  analyzing: "文档分析",
  translating: "翻译与审校",
  exporting: "导出",
  finished: "完成",
  failed: "失败",
  cancelled: "已取消",
};
export const chipClass = (s: TaskStatus) =>
  `chip ${s === "completed" ? "chip-done" : s === "partial_success" ? "chip-warn" : s === "failed" ? "chip-fail" : s === "processing" ? "chip-running" : "chip-pending"}`;
