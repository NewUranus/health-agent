import { apiFetch } from "./api";
import { createSseParser } from "./sse";
import type { ServerEvent } from "../types";

export interface SavedTask {
  id: string; thread_id: string; status: string; request_id: string;
  created_at: number; updated_at: number; error_code: string | null; cancel_requested: boolean;
  health_committed?: boolean | null; failure_category?: string | null;
  review_disposition?: "resolved" | "needs_support" | null;
}

export async function responseProblem(response: Response): Promise<Error> {
  const body = await response.json().catch(() => ({}));
  return new Error(`连接请求未完成：${body.detail ?? "服务暂不可用"}${body.request_id ? `（请求编号：${body.request_id}）` : ""}`);
}

export async function runTask(payload: { thread_id: string; message: string; web_search: boolean }, key: string,
  signal: AbortSignal, onEvent: (event: ServerEvent) => void) {
  let task: SavedTask | undefined;
  // 提交响应丢失时重用同一幂等键，服务端不会新建第二个任务。
  for (let attempt = 0; attempt < 2; attempt++) {
    let response: Response;
    try {
      response = await apiFetch("/api/v1/tasks", { method: "POST", signal,
        headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify(payload) });
    } catch (error) {
      if (signal.aborted || attempt === 1) throw error;
      continue;
    }
    if (!response.ok) throw await responseProblem(response);
    task = await response.json();
    break;
  }
  if (!task?.id) throw new Error("连接响应无效，请在任务记录中核对提交结果。");
  let cursor = 0;
  for (let attempt = 0; attempt < 3; attempt++) {
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let doneSeen = false;
    let retryable = true;
    try {
      const response = await apiFetch(`/api/v1/tasks/${task.id}/events?after=${cursor}`, { signal });
      if (!response.ok || !response.body) {
        retryable = response.status >= 500;
        throw await responseProblem(response);
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = createSseParser(event => {
        if (signal.aborted) return;
        const sequence = "seq" in event && typeof event.seq === "number" ? event.seq : null;
        if (sequence !== null && sequence <= cursor) return;
        if (sequence !== null) cursor = sequence;
        if (event.type === "done") doneSeen = true;
        onEvent(event);
      });
      while (true) {
        const { value, done } = await reader.read();
        parser.push(decoder.decode(value, { stream: !done }));
        if (doneSeen) return;
        if (done) { parser.flush(); break; }
      }
      if (doneSeen) return;
      throw new Error("连接中断，任务可能仍在执行，请在任务记录中查看。");
    } catch (error) {
      if (signal.aborted || !retryable || attempt === 2) throw error;
    } finally {
      if (reader) { void reader.cancel().catch(() => {}); reader.releaseLock(); }
    }
  }
}
