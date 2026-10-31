import { describe, expect, it } from "vitest";
import { createSseParser } from "./sse";

function collect() {
  const events: unknown[] = [];
  return { events, parser: createSseParser((e) => events.push(e)) };
}

describe("createSseParser", () => {
  it("解析多个事件", () => {
    const { events, parser } = collect();
    parser.push('data: {"type":"node"}\n\ndata: {"type":"done"}\n\n');
    expect(events).toEqual([{ type: "node" }, { type: "done" }]);
  });

  it("事件被切成任意片段时也能还原", () => {
    const { events, parser } = collect();
    for (const ch of 'data: {"type":"message","content":"你好"}\n\n') parser.push(ch);
    expect(events).toEqual([{ type: "message", content: "你好" }]);
  });

  it("兼容 CRLF", () => {
    const { events, parser } = collect();
    parser.push('data: {"type":"done"}\r\n\r\n');
    expect(events).toEqual([{ type: "done" }]);
  });

  it("CRLF 被切开时也能还原", () => {
    const { events, parser } = collect();
    parser.push('data: {"type":"done"}\r');
    parser.push("\n\r");
    parser.push("\n");
    expect(events).toEqual([{ type: "done" }]);
  });

  it("没有结尾空行的事件在 flush 时处理", () => {
    const { events, parser } = collect();
    parser.push('data: {"type":"done"}');
    expect(events).toEqual([]);
    parser.flush();
    expect(events).toEqual([{ type: "done" }]);
  });

  it("忽略注释行和空事件", () => {
    const { events, parser } = collect();
    parser.push(': keep-alive\n\n\n\ndata: {"type":"done"}\n\n');
    expect(events).toEqual([{ type: "done" }]);
  });

  it("多行 data 拼接后解析", () => {
    const { events, parser } = collect();
    parser.push('data: {"type":\ndata: "done"}\n\n');
    expect(events).toEqual([{ type: "done" }]);
  });

  it("JSON 非法时抛错", () => {
    const { parser } = collect();
    expect(() => parser.push("data: {oops\n\n")).toThrow();
  });
});
