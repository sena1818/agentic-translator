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
  getStructure: vi.fn(),
  cancelTranslation: vi.fn(),
  editChunk: vi.fn(),
  markReviewed: vi.fn(),
  previewTermCorrection: vi.fn(),
  applyTermCorrection: vi.fn(),
  getTermSuggestions: vi.fn(async () => ({ items: [] })),
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
it("问题段在应用内阅读页定位，账本可去术语表审阅，阅读页一次点击进入", async () => {
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
  expect(locate).toHaveAttribute("href", "/tasks/demo/read?seg=41");
  expect(
    screen.getByText(/术语账本 98 条（本次新增 3 条）/),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "去术语表审阅 →" })).toHaveAttribute(
    "href",
    "/glossary",
  );
  expect(screen.getByRole("link", { name: "对照阅读" })).toHaveAttribute(
    "href",
    "/tasks/demo/read",
  );
});

it("有 EPUB 时提供两种下载，缺图时列出找不到的图片", async () => {
  vi.mocked(api.getTaskStatus).mockResolvedValue({
    ...task,
    quality_summary: {
      total: 1,
      failed: 0,
      needs_review: 0,
      repaired: 0,
      structured: 1,
      segments: 3,
      dead_links: 0,
      missing_images: 1,
      epub: 2,
    },
  });
  vi.mocked(api.getChunks).mockResolvedValue({
    total: 0,
    revision: "v",
    chunks: [],
  });
  vi.mocked(api.getStructure).mockResolvedValue({
    task_id: "demo",
    segments: 3,
    dead_links: [],
    missing_images: ["images/lost.png"],
    epub: ["mono", "bilingual"],
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
  expect(
    await screen.findByRole("link", { name: "中文 EPUB" }),
  ).toHaveAttribute(
    "href",
    expect.stringContaining("format=epub&variant=mono"),
  );
  expect(screen.getByRole("link", { name: "双语 EPUB" })).toHaveAttribute(
    "href",
    expect.stringContaining("format=epub&variant=bilingual"),
  );
  expect(await screen.findByText("images/lost.png")).toBeInTheDocument();
  expect(screen.getByText(/1 张图片找不到/)).toBeInTheDocument();
});
it("改掉术语译名后预览全书其余出现处，勾选后批量应用；建议标明来源", async () => {
  vi.mocked(api.getTaskStatus).mockResolvedValue({
    ...task,
    author: "Nick Land",
    domain: null,
    quality_summary: {
      total: 1,
      failed: 0,
      needs_review: 0,
      repaired: 0,
      structured: 1,
      segments: 3,
    },
  });
  vi.mocked(api.getChunks).mockResolvedValue({
    total: 1,
    revision: "v1",
    chunks: [
      {
        chunk_index: 0,
        chunk_id: "one",
        original: "The Numogram maps zones.",
        translation: "数图映射区域。",
        success: true,
        cached: false,
        repaired: false,
        quality_report: { passed: true, edited: true },
      },
    ],
  });
  vi.mocked(api.getTermSuggestions).mockResolvedValue({
    items: [
      {
        term: "Oil",
        rendering: "石油",
        book: "Cyclonopedia",
        scope: "author",
        source: "human",
        label: "同作者《Cyclonopedia》·人工纠正",
      },
    ],
  });
  vi.mocked(api.editChunk).mockResolvedValue({
    total: 1,
    revision: "v2",
    chunks: [],
    term_changes: [
      {
        term: "Numogram",
        old_rendering: "数图",
        new_rendering: "数字图",
        count: 1,
      },
    ],
  });
  vi.mocked(api.previewTermCorrection).mockResolvedValue({
    term: "Numogram",
    old: "数图",
    new: "数字图",
    total_segments: 2,
    total_replacements: 2,
    items: [
      {
        position: 0,
        seg_id: 1,
        chunk_index: 0,
        source: "The Numogram",
        translation: "数图",
        preview: "数字图",
        count: 1,
      },
      {
        position: 4,
        seg_id: 5,
        chunk_index: 1,
        source: "Numogram again",
        translation: "又是数图",
        preview: "又是数字图",
        count: 1,
      },
    ],
    skipped: [
      { position: 2, seg_id: 3, chunk_index: 0, reason: "already_new" },
    ],
    revision: "v2",
  });
  vi.mocked(api.applyTermCorrection).mockResolvedValue({
    replacements: 1,
    applied_segments: [4],
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
  const user = userEvent.setup();
  expect(await screen.findByText("人工编辑过")).toBeInTheDocument();
  await user.click(await screen.findByText(/来自其他书的术语建议 1 条/));
  expect(
    screen.getByText("（同作者《Cyclonopedia》·人工纠正）"),
  ).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "编辑译文" }));
  await user.click(screen.getByRole("button", { name: "保存审校" }));
  expect(await screen.findByRole("textbox", { name: "新译名" })).toHaveValue(
    "数字图",
  );
  await user.click(screen.getByRole("button", { name: "预览出现处" }));
  expect(await screen.findByText("又是数字图")).toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: "应用到段 1" }));
  await user.click(screen.getByRole("button", { name: "应用到选中的 1 段" }));
  expect(api.applyTermCorrection).toHaveBeenCalledWith(
    "demo",
    { term: "Numogram", old: "数图", new: "数字图" },
    [4],
    "v2",
  );
});
