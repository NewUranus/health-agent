import { describe, expect, it } from "vitest";
import { recoveryMode, recoveryNote } from "../src/lib/recovery";

describe("失败恢复", () => {
  it("没有任务结果时继续查看原幂等键", () => {
    expect(recoveryMode(null)).toBe("reconnect");
    expect(recoveryNote("reconnect", null)).toContain("同一幂等键");
  });

  it("未写入健康记录的失败任务允许重新提交", () => {
    expect(recoveryMode({ status: "failed", health_committed: false })).toBe("resubmit");
    expect(recoveryMode({ status: "interrupted", health_committed: false })).toBe("resubmit");
  });

  it("已写入或无法确认时只转人工核对", () => {
    expect(recoveryMode({ status: "failed", health_committed: true })).toBe("hold");
    expect(recoveryMode({ status: "failed", health_committed: null })).toBe("hold");
    expect(recoveryNote("hold", true)).toContain("不要直接重发");
  });
});
