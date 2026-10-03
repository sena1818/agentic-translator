import { expect, it, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import PageBoundary from "./PageBoundary";

it("页面资源加载失败时显示恢复入口", () => {
  const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
  function BrokenPage(): never {
    throw new Error("Failed to fetch dynamically imported module");
  }
  try {
    render(
      <PageBoundary>
        <BrokenPage />
      </PageBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("页面加载失败");
    expect(
      screen.getByRole("button", { name: "刷新页面" }),
    ).toBeInTheDocument();
  } finally {
    cleanup();
    errorLog.mockRestore();
  }
});
