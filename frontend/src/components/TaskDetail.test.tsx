import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "antd";
import TaskDetail from "./TaskDetail";
import * as api from "../services/api";

vi.mock("../services/api", async () => ({
  ...(await vi.importActual("../services/api")),
  getTaskStatus: vi.fn(),
  getChunks: vi.fn(),
  cancelTranslation: vi.fn(),
  editChunk: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
const task: api.TranslationTask = {
  task_id: "demo",
  filename: "book.md",
  status: "completed",
  glossary_id: null,
  bilingual: true,
  progress: { current: 1, total: 1, percentage: 100, speed: 1, elapsed: 60 },
  result_url: "/api/files/results/demo",
  error: null,
  created_at: "2026-10-03T08:00:00Z",
  updated_at: "2026-10-03T08:00:00Z",
  stage: "finished",
  quality_summary: { total: 1, failed: 0, needs_review: 0, repaired: 0 },
};
function mount(status: api.TaskStatus = "completed") {
  vi.mocked(api.getTaskStatus).mockResolvedValue({ ...task, status });
  vi.mocked(api.getChunks).mockResolvedValue({
    total: 1,
    revision: "version-1",
    chunks: [
      {
        chunk_index: 0,
        chunk_id: "one",
        original: "```python\nprint(1)\n```",
        translation: "初始译文",
        success: true,
        cached: false,
        repaired: false,
        quality_report: { passed: true },
      },
    ],
  });
  return render(
    <MemoryRouter initialEntries={["/tasks/demo"]}>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <App>
          <Routes>
            <Route path="/tasks/:taskId" element={<TaskDetail />} />
          </Routes>
        </App>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}
it("从任务地址恢复详情，正确渲染代码并在保存失败时保留草稿", async () => {
  vi.mocked(api.editChunk).mockRejectedValue(new Error("版本已更新"));
  mount();
  expect(await screen.findByText("print(1)")).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "编辑译文" }));
  const editor = screen.getByRole("textbox", { name: "块 1 译文" });
  await user.clear(editor);
  await user.type(editor, "手工修改");
  await user.click(screen.getByRole("button", { name: "保存审校" }));
  expect(await screen.findByText("版本已更新")).toBeInTheDocument();
  expect(editor).toHaveValue("手工修改");
});
it("取消后刷新状态并提供恢复入口", async () => {
  mount("processing");
  vi.mocked(api.cancelTranslation).mockImplementation(async () => {
    vi.mocked(api.getTaskStatus).mockResolvedValue({
      ...task,
      status: "cancelled",
      stage: "cancelled",
    });
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "取消任务" }));
  expect(
    await screen.findByRole("button", { name: "恢复任务" }),
  ).toBeInTheDocument();
  expect(api.cancelTranslation).toHaveBeenCalledWith("demo");
});
it("结构化导出的任务提供对照阅读器入口，并替代旧双栏 HTML", async () => {
  vi.mocked(api.getTaskStatus).mockResolvedValue({
    ...task,
    quality_summary: {
      total: 1,
      failed: 0,
      needs_review: 0,
      repaired: 0,
      structured: 1,
      segments: 12,
      dead_links: 0,
    },
  });
  vi.mocked(api.getChunks).mockResolvedValue({
    total: 0,
    revision: "v",
    chunks: [],
  });
  render(
    <MemoryRouter initialEntries={["/tasks/demo"]}>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <App>
          <Routes>
            <Route path="/tasks/:taskId" element={<TaskDetail />} />
          </Routes>
        </App>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  const reader = await screen.findByRole("link", { name: "新窗口打开阅读器" });
  expect(reader).toHaveAttribute(
    "href",
    expect.stringContaining("format=html&inline=true"),
  );
  expect(reader).toHaveAttribute("target", "_blank");
  expect(
    screen.queryByRole("link", { name: "双栏 HTML" }),
  ).not.toBeInTheDocument();
  expect(screen.getByText(/12 段对齐/)).toBeInTheDocument();
});
it("问题段可在对照阅读器中定位，账本可去术语表审阅，阅读器可在页面内展开", async () => {
  vi.mocked(api.getTaskStatus).mockResolvedValue({
    ...task,
    quality_summary: {
      total: 1,
      failed: 0,
      needs_review: 1,
      repaired: 0,
      structured: 1,
      segments: 9,
      dead_links: 0,
      ledger_terms: 98,
      ledger_terms_added: 3,
    },
  });
  vi.mocked(api.getChunks).mockResolvedValue({
    total: 1,
    revision: "v",
    chunks: [
      {
        chunk_index: 0,
        chunk_id: "one",
        original: "Kaye",
        translation: "凯",
        success: true,
        cached: false,
        repaired: false,
        quality_report: {
          passed: false,
          issues: [
            {
              kind: "terminology",
              severity: "medium",
              message: "[段 3] 术语未统一",
              segment: 41,
            },
          ],
        },
      },
    ],
  });
  render(
    <MemoryRouter initialEntries={["/tasks/demo"]}>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <App>
          <Routes>
            <Route path="/tasks/:taskId" element={<TaskDetail />} />
          </Routes>
        </App>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  const locate = await screen.findByRole("link", {
    name: "在对照阅读器中定位",
  });
  expect(locate).toHaveAttribute("href", expect.stringContaining("#seg-41"));
  expect(
    screen.getByText(/术语账本 98 条（本次新增 3 条）/),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "去术语表审阅 →" })).toHaveAttribute(
    "href",
    "/glossary",
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "在页面中对照阅读" }));
  expect(screen.getByTitle("对照阅读器")).toHaveAttribute(
    "src",
    expect.stringContaining("inline=true"),
  );
});
