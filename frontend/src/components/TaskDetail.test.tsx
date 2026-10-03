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
