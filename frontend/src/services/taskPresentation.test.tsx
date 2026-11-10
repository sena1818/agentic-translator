import { expect, it } from "vitest";
import { toCommonMark } from "./taskPresentation";

it("把 pandoc 专有语法转成审校界面可渲染的 Markdown", () => {
  const input = [
    "# 利莫里亚时间战争 {#lemurian-time-war}",
    "",
    "::: {#ch1 .section}",
    "[]{#anchor}情报来源^1^传递给Ccru，见注^12^。",
    "[强调]{.calibre5} 与 H^+^",
    ":::",
  ].join("\n");
  const output = toCommonMark(input);
  expect(output).toContain("# 利莫里亚时间战争\n");
  expect(output).not.toContain("{#");
  expect(output).not.toContain(":::");
  expect(output).toContain("情报来源¹传递给Ccru，见注¹²。");
  expect(output).toContain("强调 与 H+");
});
