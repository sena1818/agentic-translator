import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "antd";
import TranslationPage from "./TranslationPage";
import * as api from "../services/api";

vi.mock("../services/api", async () => ({
  ...(await vi.importActual("../services/api")),
  getSystemState: vi.fn(),
  getGlossaryList: vi.fn(),
  getTaskList: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
function mount() {
  vi.mocked(api.getSystemState).mockResolvedValue({
    workers_online: 0,
    formats: ["md"],
    max_upload_bytes: 50 * 1024 * 1024,
  });
  vi.mocked(api.getGlossaryList).mockResolvedValue([]);
  return render(
    <MemoryRouter>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <App>
          <TranslationPage />
        </App>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}
describe("任务工作台", () => {
  it("将连接失败与空列表区分", async () => {
    vi.mocked(api.getTaskList).mockRejectedValue(new Error("offline"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "任务列表加载失败",
    );
    expect(screen.queryByText("没有匹配的任务")).not.toBeInTheDocument();
  });
  it("显示取消状态，并使用服务端全量统计", async () => {
    vi.mocked(api.getTaskList).mockResolvedValue({
      total: 100,
      stats: { completed: 99, cancelled: 1 },
      tasks: [
        {
          task_id: "demo",
          filename: "demo.md",
          status: "cancelled",
          glossary_id: null,
          bilingual: false,
          progress: {
            current: 1,
            total: 2,
            percentage: 50,
            speed: 1,
            elapsed: 60,
          },
          result_url: null,
          error: null,
          created_at: "2026-10-03T08:00:00Z",
          updated_at: "2026-10-03T08:00:00Z",
          stage: "cancelled",
          quality_summary: {},
        },
      ],
    });
    mount();
    expect(await screen.findByText("demo.md")).toHaveAttribute(
      "href",
      "/tasks/demo",
    );
    expect(screen.getAllByText("已取消").length).toBeGreaterThan(0);
    expect(screen.getByText("99")).toBeInTheDocument();
    expect(await screen.findByText(/Worker 离线/)).toBeInTheDocument();
  });
});
