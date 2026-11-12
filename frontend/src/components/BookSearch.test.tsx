import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import BookSearch from "./BookSearch";
import * as api from "../services/api";

vi.mock("../services/api", async () => ({
  ...(await vi.importActual("../services/api")),
  searchBookByMeaning: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const hit = (over: Partial<api.MeaningHit>): api.MeaningHit => ({
  position: 3,
  seg_id: 12,
  section_path: ["第一章"],
  source: "Petroleum seeps up through the desert.",
  translation: "石油从沙漠里渗出。",
  score: 0.03,
  lexical_rank: 1,
  vector_rank: 2,
  similarity: 0.71,
  ...over,
});

function mount() {
  render(
    <MemoryRouter>
      <QueryClientProvider client={new QueryClient()}>
        <BookSearch taskId="demo" />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function submit(text: string) {
  fireEvent.change(screen.getByLabelText("检索内容"), {
    target: { value: text },
  });
  fireEvent.click(screen.getByRole("button", { name: "检索" }));
}

it("shows source and translation and links to the reader position", async () => {
  vi.mocked(api.searchBookByMeaning).mockResolvedValue({
    query: "原油从哪来",
    mode: "hybrid",
    model: "fake",
    hint: "",
    items: [hit({}), hit({ position: 4, seg_id: null, source: "Kept." })],
  });
  mount();
  submit("原油从哪来");
  expect(await screen.findByText("Kept.")).toBeTruthy();
  expect(api.searchBookByMeaning).toHaveBeenCalledWith("demo", "原油从哪来");
  const link = screen.getByRole("link", { name: "在对照阅读中定位" });
  expect(link.getAttribute("href")).toBe("/tasks/demo/read?seg=12");
  expect(screen.getByText("保留原文的段，阅读器中无对照位置")).toBeTruthy();
  expect(screen.queryByRole("status")).toBeNull();
});

it("tells the reader when only keyword search is available", async () => {
  vi.mocked(api.searchBookByMeaning).mockResolvedValue({
    query: "oil",
    mode: "lexical",
    model: null,
    hint: "未配置向量模型，当前为仅关键词检索",
    items: [],
  });
  mount();
  submit("oil");
  expect((await screen.findByRole("status")).textContent).toContain(
    "未配置向量模型",
  );
  expect(screen.getByText("没有找到相近的段落。")).toBeTruthy();
});
