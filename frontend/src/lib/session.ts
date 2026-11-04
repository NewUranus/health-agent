import type { FollowupStatus, Message, NodeData, Risk, ServerEvent, TraceTurn } from "../types";
import type { ConversationTurn } from "./conversations";
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
  profilePlaceholder: string;
  risk: Risk | null;
  citations: Map<string, number>;
  followup: FollowupStatus;
  lastNode: string | null;
}

export type SessionAction =
  | { type: "restore_conversation"; threadId: string; turns: ConversationTurn[] }
  | { type: "send"; text: string }
  | { type: "event"; event: ServerEvent }
  | { type: "finish"; status: string }
  | { type: "followup"; value: FollowupStatus; threadId: string; userId: string }
  | { type: "restore"; threadId: string; events: ServerEvent[]; waiting: boolean }
  | { type: "reset"; threadId: string; userId?: string };

export function initialSession(userId?: string): Session {
  return freshSession(createId(), userId ?? loadUserId());
}

function freshSession(threadId: string, userId: string): Session {
  return {
    threadId, userId, turn: 0, busy: false,
    status: "你的每一步，都有依据可查",
    messages: [{ role: "assistant", text: welcomeText, turn: 0 }],
    traces: [], intake: null, risk: null, citations: new Map(), followup: { status: "none" }, lastNode: null,
    profilePlaceholder: "在对话中告诉我年龄、血压和血脂，逐步完善你的档案。",
  };
}

export function sessionReducer(state: Session, action: SessionAction): Session {
  switch (action.type) {
    case "restore_conversation": {
      let restored = freshSession(action.threadId, state.userId);
      for (const turn of action.turns) {
        restored = sessionReducer(restored, { type: "send", text: turn.message });
        for (const event of turn.events) restored = sessionReducer(restored, { type: "event", event });
      }
      return { ...restored, busy: false, status: "已读取保存的会话记录。继续提问时服务端会使用该会话的最新上下文。" };
    }
    case "restore": {
      const base: Session = { ...freshSession(action.threadId, state.userId), turn: 1, messages: [], traces: [{ turn: 1, nodes: [] }] };
      const restored = action.events.reduce((value, event) => sessionReducer(value, { type: "event", event }), base);
      return { ...restored, busy: false, lastNode: action.waiting ? "checkin_question" : restored.lastNode,
        status: action.waiting ? '请回答上面的随访问题（回复"取消"可退出）' : "已切换到保存的会话，可以继续提问。" };
    }
    case "followup":
      return action.threadId === state.threadId && action.userId === state.userId
        ? { ...state, followup: action.value } : state;
    case "reset":
      return {
        ...freshSession(action.threadId, action.userId ?? state.userId),
        status: action.userId ? "已更换身份（服务端历史不会删除）" : "已开启新会话",
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
      return { ...state, busy: false, status: state.lastNode === "checkin_question"
        ? '请回答上面的随访问题（回复"取消"可退出）' : action.status };
    case "event": {
      const event = action.event;
      if (event.type === "done") return state;
      if (event.type === "message" || event.type === "error") {
        const text = event.type === "message" ? event.content
          : `${event.message}${event.next_action ? `\n${event.next_action}` : ""}${event.request_id ? `\n请求编号：${event.request_id}` : ""}`;
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
        ...state, risk, lastNode: event.node, status: event.node === "checkin_question"
          ? '请回答上面的随访问题（回复"取消"可退出）' : progressLabel(event.node, data),
        // 恢复执行不经过 intake，问诊里改过的档案（如戒烟）随 checkin 事件带回来
        intake: event.node === "intake" ? data
          : event.node === "checkin" && data.profile ? { ...state.intake, profile: data.profile, missing: [] } : state.intake,
        traces: state.traces.map((trace) => trace.turn === state.turn
          ? { ...trace, nodes: [...trace.nodes, event] } : trace),
      };
    }
  }
}
