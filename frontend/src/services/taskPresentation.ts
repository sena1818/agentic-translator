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

const SUPERSCRIPT_DIGITS: Record<string, string> = {
  "0": "⁰",
  "1": "¹",
  "2": "²",
  "3": "³",
  "4": "⁴",
  "5": "⁵",
  "6": "⁶",
  "7": "⁷",
  "8": "⁸",
  "9": "⁹",
};

/**
 * 结构化翻译的块文本是 pandoc Markdown；审校界面的 react-markdown 只认 CommonMark/GFM。
 * 渲染前去掉 pandoc 专有的属性与围栏，并把数字上标（尾注引用 ^1^）转成 Unicode 上标。
 */
export function toCommonMark(text: string): string {
  return text
    .replace(/^(#{1,6}\s.*?)\s*\{[#.][^}\n]*\}\s*$/gm, "$1")
    .replace(/\[\]\{[^}\n]*\}/g, "")
    .replace(/\[([^\]\n]+)\]\{\.[^}\n]*\}/g, "$1")
    .replace(/^\s*:::.*$/gm, "")
    .replace(/\^(\d+)\^/g, (_, digits: string) =>
      digits
        .split("")
        .map((d) => SUPERSCRIPT_DIGITS[d])
        .join(""),
    )
    .replace(/\^([^\s^]+)\^/g, "$1");
}
