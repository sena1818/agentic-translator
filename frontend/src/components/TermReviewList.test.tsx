import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TermReviewList from "./TermReviewList";
import * as api from "../services/api";

vi.mock("../services/api", async () => ({
  ...(await vi.importActual("../services/api")),
  getTermReviews: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mount() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <TermReviewList />
    </QueryClientProvider>,
  );
}

it("lists terms the agent handed off with their book, reason and contexts", async () => {
  vi.mocked(api.getTermReviews).mockResolvedValue([
    {
      task_id: "t1",
      book: "Ccru",
      term: "Gates",
      reason: "agent:门还是人名",
      contexts: ["…the Gates of the Numogram…"],
      evidence: [],
      steps: 3,
    },
  ]);
  mount();
  expect(await screen.findByText("术语 agent 转交待审（1）")).toBeTruthy();
  expect(screen.getByText("Gates")).toBeTruthy();
  expect(screen.getByText(/《Ccru》· agent:门还是人名/)).toBeTruthy();
  expect(screen.getByText("…the Gates of the Numogram…")).toBeTruthy();
});

it("renders nothing when there is nothing to review", async () => {
  vi.mocked(api.getTermReviews).mockResolvedValue([]);
  const { container } = mount();
  await vi.waitFor(() => expect(api.getTermReviews).toHaveBeenCalled());
  expect(container.textContent).toBe("");
});
