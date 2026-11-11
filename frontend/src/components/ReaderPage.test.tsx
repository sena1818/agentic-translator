import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ReaderPage from "./ReaderPage";
import * as api from "../services/api";

vi.mock("../services/api", async () => ({
  ...(await vi.importActual("../services/api")),
  getTaskStatus: vi.fn(),
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

function mount(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <Routes>
          <Route path="/tasks/:taskId/read" element={<ReaderPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

it("阅读页地址直接打开对照阅读器，并定位到地址里的段", async () => {
  vi.mocked(api.getTaskStatus).mockResolvedValue({
    ...task,
    quality_summary: { ...task.quality_summary, structured: 1 },
  });
  mount("/tasks/demo/read?seg=41");
  const frame = await screen.findByTitle("对照阅读器");
  expect(frame).toHaveAttribute(
    "src",
    expect.stringMatching(/format=html&inline=true#seg-41$/),
  );
  expect(frame).toHaveAttribute(
    "sandbox",
    "allow-scripts allow-popups allow-popups-to-escape-sandbox",
  );
  expect(screen.getByRole("link", { name: "← 任务详情" })).toHaveAttribute(
    "href",
    "/tasks/demo",
  );
  expect(screen.getByRole("heading", { name: "book.md" })).toBeInTheDocument();
});

it("历史任务没有阅读器时给出提示，不报错", async () => {
  vi.mocked(api.getTaskStatus).mockResolvedValue(task);
  mount("/tasks/demo/read");
  expect(
    await screen.findByText(/历史任务可以在任务详情里下载已有文件/),
  ).toBeInTheDocument();
  expect(screen.queryByTitle("对照阅读器")).not.toBeInTheDocument();
});
