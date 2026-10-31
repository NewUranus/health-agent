import type { ServerEvent } from "../types";

export function createSseParser(onEvent: (event: ServerEvent) => void): {
  push(chunk: string): void;
  flush(): void;
} {
  let buffer = "";
  let data: string[] = [];

  function dispatch() {
    if (!data.length) return;
    const payload = data.join("\n");
    data = [];
    onEvent(JSON.parse(payload) as ServerEvent);
  }

  function consumeLine(line: string) {
    // 按完整行处理，避免分块恰好切开 CRLF 时丢失事件边界。
    if (line.endsWith("\r")) line = line.slice(0, -1);
    if (!line) dispatch();
    else if (line.startsWith("data:")) {
      data.push(line.slice(5).replace(/^ /, ""));
    }
  }

  return {
    push(chunk) {
      buffer += chunk;
      let end: number;
      while ((end = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, end);
        buffer = buffer.slice(end + 1);
        consumeLine(line);
      }
    },
    flush() {
      if (buffer) consumeLine(buffer);
      buffer = "";
      dispatch();
    },
  };
}
