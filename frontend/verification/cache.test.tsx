import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Operations } from "../src/admin/Operations";

const overview = {
  release: "local", tasks: {}, workers: [], oldest_pending_seconds: 0, stale_tasks: 0, completed_24h: 0,
  average_completion_seconds_24h: 0, needs_support: 0, admission_paused: false,
  costs_24h: { cost_microusd: null, priced_calls: 0 }, alerts: [], failures_24h: [],
  cache: { tools: { hits: 4, misses: 2, saved: 4 }, vectors: { hits: 3, misses: 1, saved: 3 } },
};

describe("计算缓存", () => {
  it("运维页显示命中、未命中和省下的调用", async () => {
    const api = vi.fn(async (path: string) => {
      if (path === "/operations") return overview;
      if (path === "/queue") return { tasks: [] };
      if (path === "/support") return { tasks: [] };
      return { records: [] };
    });
    render(<Operations api={api} />);
    expect(await screen.findByText("风险计算命中 / 未命中 / 省下的调用")).toBeInTheDocument();
    expect(screen.getByText("4 / 2 / 4")).toBeInTheDocument();
    expect(screen.getByText("3 / 1 / 3")).toBeInTheDocument();
  });
});
