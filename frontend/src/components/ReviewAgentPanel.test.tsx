import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ReviewAgentPanel from "./ReviewAgentPanel";
import * as api from "../services/api";

vi.mock("../services/api", async () => ({
  ...(await vi.importActual("../services/api")),
  getReviewAgent: vi.fn(),
  runReviewAgent: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const item = (over: Partial<api.TriageItem>): api.TriageItem => ({
  issue: {
    issue_id: "q1",
    seg_id: 4,
    chunk_index: 0,
    kind: "terminology",
    severity: "medium",
    message: "术语 `Numogram` 未体现为预期译名 `数图`",
    origin: "quality",
  },
  verdict: "fixed",
  label: "已修",
  reason: "",
  edits: [{ from: "数字图", to: "数图" }],
  before: "数字图映射十进制数。",
  after: "数图映射十进制数。",
  by: "agent",
  steps: 3,
  tokens: 900,
  tool_calls: 2,
  ...over,
});

const run: api.TriageRun = {
  finished_at: "2026-11-12T00:00:00+00:00",
  summary: {
    issues: 2,
    by_verdict: { fixed: 1, dismissed: 0, handoff: 1 },
    tokens: 1500,
    steps: 5,
    max_tokens_per_issue: 900,
    seconds: 1,
  },
  results: [
    item({}),
    item({
      issue: { ...item({}).issue, issue_id: "q2", seg_id: 7 },
      verdict: "handoff",
      label: "待人工",
      reason: "agent:拿不准",
    }),
  ],
};

function mount(onDone = vi.fn()) {
  render(
    <MemoryRouter>
      <App>
        <QueryClientProvider client={new QueryClient()}>
          <ReviewAgentPanel id="demo" revision="r1" onDone={onDone} />
        </QueryClientProvider>
      </App>
    </MemoryRouter>,
  );
  return onDone;
}

it("stays hidden while the review agent is disabled", async () => {
  vi.mocked(api.getReviewAgent).mockResolvedValue({
    enabled: false,
    last_run: null,
  });
  mount();
  await vi.waitFor(() => expect(api.getReviewAgent).toHaveBeenCalled());
  expect(screen.queryByText("审校 agent 分诊")).toBeNull();
});

it("runs triage and lists verdicts with links to the reader", async () => {
  vi.mocked(api.getReviewAgent).mockResolvedValue({
    enabled: true,
    last_run: null,
  });
  vi.mocked(api.runReviewAgent).mockResolvedValue(run);
  const onDone = mount();
  fireEvent.click(await screen.findByRole("button", { name: "运行分诊" }));
  await vi.waitFor(() => expect(onDone).toHaveBeenCalled());
  expect(api.runReviewAgent).toHaveBeenCalledWith("demo", "r1");

  vi.mocked(api.getReviewAgent).mockResolvedValue({
    enabled: true,
    last_run: run,
  });
  cleanup();
  mount();
  expect(await screen.findByText("待人工")).toBeTruthy();
  expect(screen.getByText("agent:拿不准")).toBeTruthy();
  const links = screen.getAllByRole("link");
  expect(links[0].getAttribute("href")).toBe("/tasks/demo/read?seg=7"); // 待人工排在前面
});
