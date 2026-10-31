import type { Message, NodeData, Risk, ServerEvent, TraceTurn } from "../types";
import { progressLabel } from "./progress";

export const welcomeText = "你好，我是 HealthPilot。\n我们可以一起了解你的心血管健康，从年龄、性别和最近的血压开始。资料齐全后，我会提供风险评估、有来源的建议和随访计划。\n也可以点击下方示例，体验完整流程。";

export const createId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;

export function saveUserId(id: string) {
  try { localStorage.setItem("healthpilot-user", id); } catch { /* 存储受限时继续使用内存身份。 */ }
}

export function loadUserId(): string {
  let id: string | null = null;
  try { id = localStorage.getItem("healthpilot-user"); } catch { /* 存储受限时创建临时身份。 */ }
  id ||= createId();
  saveUserId(id);
  return id;
}

interface Session {
  threadId: string;
  userId: string;
  turn: number;
  busy: boolean;
  status: string;
  messages: Message[];
  traces: TraceTurn[];
  intake: NodeData | null;
  profileStatus: string;
  profilePlaceholder: string;
  risk: Risk | null;
  citations: Map<string, number>;
}

export type SessionAction =
  | { type: "send"; text: string }
  | { type: "event"; event: ServerEvent }
  | { type: "finish"; status: string }
  | { type: "reset"; threadId: string; userId?: string };

export function initialSession(): Session {
  return freshSession(createId(), loadUserId());
}

function freshSession(threadId: string, userId: string): Session {
  return {
    threadId, userId, turn: 0, busy: false,
    status: "你的每一步，都有依据可查",
    messages: [{ role: "assistant", text: welcomeText, turn: 0 }],
    traces: [], intake: null, risk: null, citations: new Map(),
    profileStatus: "等待补充",
    profilePlaceholder: "在对话中告诉我年龄、血压和血脂，逐步完善你的档案。",
  };
}

export function sessionReducer(state: Session, action: SessionAction): Session {
  switch (action.type) {
    case "reset":
      return {
        ...freshSession(action.threadId, action.userId ?? state.userId),
        status: action.userId ? "已更换身份（服务端历史不会删除）" : "已开启新会话",
        profileStatus: "等待对话",
        profilePlaceholder: action.userId
          ? "已切换为新身份，请重新提供健康信息。"
          : "新会话将在下一条消息时读取已有健康档案。",
      };
    case "send": {
      const turn = state.turn + 1;
      return {
        ...state, turn, busy: true, status: progressLabel(null),
        messages: [...state.messages, { role: "user", text: action.text, turn }],
        traces: [...state.traces, { turn, nodes: [] }],
      };
    }
    case "finish":
      return { ...state, busy: false, status: action.status };
    case "event": {
      const event = action.event;
      if (event.type === "done") return state;
      if (event.type === "message" || event.type === "error") {
        const text = event.type === "message" ? event.content : event.message;
        const citations = new Map(state.citations);
        // 来源按轮次隔离，编号在整个会话内按首次出现递增。
        for (const match of text.matchAll(/\[cite:([\w-]+)\]/g)) {
          const key = `${state.turn}:${match[1]}`;
          if (!citations.has(key)) citations.set(key, citations.size + 1);
        }
        return {
          ...state, citations,
          messages: [...state.messages, {
            role: event.type === "message" ? "assistant" : "error", text, turn: state.turn,
          }],
        };
      }
      const data = event.data ?? {};
      let risk = state.risk;
      for (const call of data.tool_calls ?? []) {
        if (call.tool === "cvd_risk" && !call.result.error && typeof call.result.risk_percent === "number") {
          risk = { risk_percent: call.result.risk_percent, level: String(call.result.level ?? "") };
        }
      }
      return {
        ...state, risk, status: progressLabel(event.node, data),
        intake: event.node === "intake" ? data : state.intake,
        traces: state.traces.map((trace) => trace.turn === state.turn
          ? { ...trace, nodes: [...trace.nodes, event] } : trace),
      };
    }
  }
}
