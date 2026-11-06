import { useContext, useEffect, useReducer, useRef, useState } from "react";
import { PasswordChange } from "./auth/PasswordChange";
import { AuthContext } from "./auth/AuthGate";
import { apiFetch, sessionExpired } from "./lib/api";
import { HealthOverview } from "./components/HealthOverview";
import { HistoryPanel } from "./components/HistoryPanel";
import { MessageContent } from "./components/MessageContent";
import { SimulationPanel } from "./components/SimulationPanel";
import { TracePanel } from "./components/TracePanel";
import { ExecutionProgress } from "./components/ExecutionProgress";
import { ConversationsPanel } from "./components/ConversationsPanel";
import { TasksPanel } from "./components/TasksPanel";
import { runTask } from "./lib/tasks";
import { recoveryMode, recoveryNote, type RecoveryMode } from "./lib/recovery";
import { createSseParser } from "./lib/sse";
import { createId, initialSession, saveUserId, sessionReducer } from "./lib/session";

type Recovery = { mode: RecoveryMode; text: string; key: string; taskId: string | null; note: string };

const examples = [
  ["开始一次风险评估 ↗", "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药"],
  ["模拟降低血压", "如果我把血压降到120，风险能降多少？"],
  ["记录一次复查", "我来复查，最近血压130/85"],
  ["急症护栏演示", "突然胸口剧痛，出冷汗"],
];

export default function App() {
  const auth = useContext(AuthContext);
  const [session, dispatch] = useReducer(sessionReducer, undefined, () => initialSession(auth.user?.id));
  const [mode, setMode] = useState("正在连接");
  const [activeTask, setActiveTask] = useState<string | null>(null);
  const [conversationArchived, setConversationArchived] = useState(false);
  const [cancelMessage, setCancelMessage] = useState("");
  const [recovery, setRecovery] = useState<Recovery | null>(null);
  const [input, setInput] = useState("");
  const [webSearch, setWebSearch] = useState(false);
  const [traceVisible, setTraceVisible] = useState(true);
  const [panelTab, setPanelTab] = useState<"trace" | "simulate" | "history" | "tasks" | "conversations">("trace");
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const [selection, setSelection] = useState(0);
  const messages = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const sidePanel = useRef<HTMLElement>(null);
  const traceCount = session.traces.reduce((total, turn) => total + turn.nodes.length, 0);

  useEffect(() => {
    const controller = new AbortController();
    apiFetch("/api/health", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json();
      })
      .then((data) => { if (!controller.signal.aborted) setMode(data.mock ? "● Mock 演示模式" : "● 千问真实模式"); })
      .catch(() => { if (!controller.signal.aborted) setMode("服务暂不可用"); });
    return () => {
      controller.abort();
      activeRequest.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (session.busy) return;
    const controller = new AbortController();
    apiFetch(`/api/followup?user_id=${encodeURIComponent(session.userId)}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json();
      })
      .then((value) => {
        if (!controller.signal.aborted) dispatch({ type: "followup", value, threadId: session.threadId, userId: session.userId });
      })
      .catch(() => { /* 提醒请求失败不影响对话。 */ });
    return () => controller.abort();
  }, [session.userId, session.threadId, session.busy, session.turn]);

  useEffect(() => {
    if (messages.current) messages.current.scrollTop = messages.current.scrollHeight;
  }, [session.messages]);

  async function settleRecovery(text: string, key: string, taskId: string | null) {
    if (!auth.user) {
      setRecovery({ mode: "resubmit", text, key, taskId: null, note: "本轮未完成，可以重新提交。" });
      return;
    }
    if (!taskId) {
      setRecovery({ mode: "reconnect", text, key, taskId: null, note: recoveryNote("reconnect", null) });
      return;
    }
    try {
      const response = await apiFetch(`/api/v1/tasks/${taskId}`);
      if (!response.ok) throw new Error();
      const task = await response.json();
      if (task.status === "succeeded" || task.status === "waiting_human") {
        setRecovery(null);
        return;
      }
      const mode = recoveryMode(task);
      setRecovery({ mode, text, key, taskId, note: recoveryNote(mode, task.health_committed) });
    } catch {
      setRecovery({ mode: "reconnect", text, key, taskId, note: recoveryNote("reconnect", null) });
    }
  }

  async function send(text: string, reuseKey?: string) {
    text = text.trim();
    if (activeRequest.current || session.busy || conversationArchived || !text) return;
    setActiveTask(null); setCancelMessage(""); setRecovery(null);
    const controller = new AbortController();
    activeRequest.current = controller;
    const idempotencyKey = reuseKey || createId();
    let submittedTask: string | null = null;
    dispatch({ type: "send", text });
    setInput("");
    let gotDone = false;
    let gotMessage = false;
    let gotError = false;
    let failedTurn = false;
    let status = "连接未完成";
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const onEvent = (event: import("./types").ServerEvent) => {
        if (controller.signal.aborted) return;
        if (event.type === "done") gotDone = true;
        if (event.type === "message") gotMessage = true;
        if (event.type === "error") {
          gotError = true;
          failedTurn = event.code !== "AUTH_REQUIRED";
          if ("code" in event && event.code === "AUTH_REQUIRED") sessionExpired();
        }
        dispatch({ type: "event", event });
      };
      if (auth.user) {
        await runTask({ thread_id: session.threadId, message: text, web_search: webSearch }, idempotencyKey, controller.signal, onEvent, task => {
          submittedTask = task.id;
          setActiveTask(task.id);
        });
      } else {
        const response = await apiFetch("/api/chat", {
          method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
          body: JSON.stringify({ thread_id: session.threadId, user_id: session.userId, message: text, ...(webSearch ? { web_search: true } : {}) }),
        });
        if (!response.ok || !response.body) {
          const problem = await response.json().catch(() => ({}));
          throw new Error(typeof problem.detail === "string"
            ? `连接请求未完成：${problem.detail}${problem.next_action ? ` ${problem.next_action}` : ""}${problem.request_id ? `（请求编号：${problem.request_id}）` : ""}`
            : "连接暂不可用，请稍后重试。");
        }
        reader = response.body!.getReader();
        const decoder = new TextDecoder();
        const parser = createSseParser(onEvent);
        while (true) {
          const { value, done } = await reader.read();
          parser.push(decoder.decode(value, { stream: !done }));
          if (done) { parser.flush(); break; }
        }
      }
      if (!gotError && (!gotDone || !gotMessage)) {
        failedTurn = true;
        throw new Error("连接中断，回复未完成。请重试。");
      }
      status = gotError ? "本轮未完成，请检查信息后重试" : "本轮已完成 · 点击引用查看来源";
    } catch (error) {
      failedTurn = true;
      if (!controller.signal.aborted) {
        dispatch({ type: "event", event: {
          type: "error",
          message: error instanceof Error && error.message.startsWith("连接")
            ? error.message : "网络异常或回复格式错误，请稍后重试。",
        } });
      }
      controller.abort();
    } finally {
      // 取消读取并释放锁，卸载或解析出错时不留下悬挂的流。
      if (reader) {
        void reader.cancel().catch(() => {});
        reader.releaseLock();
      }
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setActiveTask(null);
        dispatch({ type: "finish", status });
        if (failedTurn) void settleRecovery(text, idempotencyKey, submittedTask);
        textarea.current?.focus();
      }
    }
  }

  async function referTask(taskId: string) {
    const response = await apiFetch(`/api/v1/tasks/${taskId}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ disposition: "needs_support" }),
    });
    if (!response.ok) {
      setCancelMessage("人工协助标记未保存，请在任务记录里重试。");
      return;
    }
    setRecovery(null);
    setCancelMessage("已提交到运维协助队列。处理完成后可在任务记录查看。");
  }

  async function stopTask() {
    if (!activeTask) return;
    try {
      const response = await apiFetch(`/api/v1/tasks/${activeTask}/cancel`, { method: "POST" });
      if (!response.ok) throw new Error("取消请求未完成，请到任务记录重试。");
      setCancelMessage("已请求停止，正在等待当前步骤结束。已经保存的记录不会回滚。");
    } catch (reason) { setCancelMessage(reason instanceof Error ? reason.message : "取消失败。"); }
  }

  function reset(clearIdentity = false) {
    if (activeRequest.current || session.busy) return;
    setWebSearch(false); setConversationArchived(false); setCancelMessage("");
    const userId = clearIdentity ? createId() : undefined;
    if (userId) saveUserId(userId);
    dispatch({ type: "reset", threadId: createId(), userId });
    setInput("");
    setHighlighted(null);
  }

  function openSimulation() {
    setTraceVisible(true);
    setPanelTab("simulate");
    // 手机上面板在对话下方，滚过去才看得到
    requestAnimationFrame(() => sidePanel.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
  }

  function selectCitation(turn: number, id: string) {
    setTraceVisible(true);
    setPanelTab("trace");
    setHighlighted(`${turn}:${id}`);
    setSelection((previous) => previous + 1);
  }

  return <>
    <header className="topbar">
      <a className="brand" href="/" aria-label="HealthPilot 首页"><span className="brand-icon" aria-hidden="true">✚</span><span>HealthPilot<small>心血管健康助手</small></span></a>
      <div className="header-actions">
        <span className="mode">{mode}</span>
        <button type="button" className="quiet" disabled={session.busy} onClick={() => reset()}>新会话 <span aria-hidden="true">＋</span></button>
        {auth.user ? <><span className="account-name">{auth.user.username}</span>
          <PasswordChange />
          <button type="button" className="quiet" disabled={auth.busy} onClick={auth.logout}>{auth.busy ? "正在退出…" : "退出登录"}</button></>
          : <button type="button" className="quiet" disabled={session.busy} onClick={() => reset(true)}>清除身份</button>}
      </div>
    </header>
    <main>
      <HealthOverview intake={session.intake} risk={session.risk} placeholder={session.profilePlaceholder} followup={session.followup} onSimulate={openSimulation} />
      {session.lastNode !== "checkin_question" && (session.followup.status === "due" || session.followup.status === "overdue") &&
        <div className="followup-reminder" role="alert">
          <span>{session.followup.status === "due" ? "已到复查时间" : `复查已逾期 ${-session.followup.days_until} 天`}</span>
          <button type="button" disabled={session.busy} onClick={() => void send("我来复查")}>开始复查</button>
        </div>}
      <div className={`workspace${traceVisible ? "" : " trace-hidden"}`}>
        <section className="chat-panel" aria-label="健康对话">
          <div className="panel-heading"><h2><span className="status-dot" /> 健康对话</h2>
            <button type="button" className="quiet" aria-expanded={traceVisible} aria-controls="trace-panel" onClick={() => setTraceVisible(!traceVisible)}>
              {traceVisible ? "收起轨迹 ↗" : "展开轨迹 ↙"}
            </button>
          </div>
          <ExecutionProgress nodes={session.traces.at(-1)?.nodes ?? []} busy={session.busy} status={session.status} />
          <div className="messages" ref={messages} aria-live="polite" aria-relevant="additions">
            {session.messages.map((message, index) => <div className={`message-row ${message.role}`} key={index}>
              {message.role !== "user" && <span className="avatar" aria-hidden="true">✚</span>}
              <div className="message-body"><div className="message-label">{message.role === "user" ? "你" : "HealthPilot"}</div>
                <article className={`message ${message.role}`}><div className="bubble">{message.role === "user" ? message.text : <MessageContent text={message.text}
                  citationNumber={(id) => session.citations.get(`${message.turn}:${id}`) ?? 0}
                  onCitationClick={(id) => selectCitation(message.turn, id)} />}</div></article>
              </div>
            </div>)}
          </div>
          <div className="composer-wrap">
            {conversationArchived && <p className="warning">此会话已归档，只能查看。请在会话列表恢复后重新打开，或新建会话。</p>}
            {recovery && !session.busy && <div className="warning" role="status">
              <p>{recovery.note}</p>
              {recovery.mode === "reconnect" && <button type="button" onClick={() => void send(recovery.text, recovery.key)}>继续查看同一任务</button>}
              {recovery.mode === "resubmit" && <button type="button" onClick={() => void send(recovery.text)}>重新提交</button>}
              {recovery.mode === "hold" && recovery.taskId && <button type="button" onClick={() => void referTask(recovery.taskId!)}>标记需要人工协助</button>}
            </div>}
            {activeTask && <button type="button" onClick={() => void stopTask()}>停止当前任务</button>}
            {cancelMessage && <p role="status">{cancelMessage}</p>}
            <label className="web-search-toggle">
              <input type="checkbox" checked={webSearch} disabled={session.busy} onChange={(event) => setWebSearch(event.target.checked)} />
              联网搜索 / 读取链接
            </label>
            {webSearch && <p className="web-search-note">本轮问题会发送给公开搜索网站；粘贴网址可直接读取。请勿填写个人身份信息。网络资料未经医学审核。</p>}
            <div className="examples" aria-label="试试这些示例">
              {examples.map(([label, text]) => <button type="button" key={label} disabled={session.busy || conversationArchived} onClick={() => void send(text)}>{label}</button>)}
            </div>
            <form id="chat-form" onSubmit={(event) => { event.preventDefault(); void send(input); }}>
              <label htmlFor="message-input" className="sr-only">输入健康信息或问题</label>
              <textarea id="message-input" disabled={conversationArchived} ref={textarea} rows={2} maxLength={12000} required
                placeholder={webSearch ? "输入要搜索的健康主题，或粘贴公开网页 / PDF 链接…" : "告诉我你的健康情况，或问一个健康问题…"} value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                    event.preventDefault(); void send(input);
                  }
                }} />
              <button className="send" type="submit" disabled={session.busy || conversationArchived} aria-label={session.busy ? "处理中…" : "发送消息"}>
                {session.busy ? "处理中…" : <>发送 <span aria-hidden="true">↑</span></>}
              </button>
            </form>
            <div className="composer-footer"><span role="status">{session.status}</span><span>Enter 发送 · Shift + Enter 换行</span></div>
          </div>
        </section>
        {traceVisible && <aside className="trace-panel" id="trace-panel" aria-label="详情" ref={sidePanel}>
          <div className="panel-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={panelTab === "trace"} onClick={() => setPanelTab("trace")}>Agent 轨迹<span>{traceCount}</span></button>
            <button type="button" role="tab" aria-selected={panelTab === "simulate"} onClick={() => setPanelTab("simulate")}>干预模拟</button>
            <button type="button" role="tab" aria-selected={panelTab === "history"} onClick={() => setPanelTab("history")}>随访记录</button>
            {auth.user && <button type="button" role="tab" aria-selected={panelTab === "conversations"} onClick={() => setPanelTab("conversations")}>我的会话</button>}
            {auth.user && <button type="button" role="tab" aria-selected={panelTab === "tasks"} onClick={() => setPanelTab("tasks")}>任务记录</button>}
          </div>
          {panelTab === "conversations" && auth.user && <ConversationsPanel busy={session.busy} onOpen={page => {
            if (activeRequest.current) return;
            dispatch({ type: "restore_conversation", threadId: page.conversation.thread_id, turns: page.turns });
            setConversationArchived(Boolean(page.conversation.archived)); setInput(""); setHighlighted(null); setWebSearch(false);
          }} />}
          {panelTab === "tasks" && auth.user && <TasksPanel busy={session.busy} onResume={(task, events) => {
            if (activeRequest.current) return;
            setConversationArchived(false);
            dispatch({ type: "restore", threadId: task.thread_id, events, waiting: task.status === "waiting_human" });
            setInput(""); setWebSearch(false); setHighlighted(null); setPanelTab("trace");
          }} />}
          {panelTab === "trace" && <TracePanel turns={session.traces} highlighted={highlighted} selection={selection} />}
          {panelTab === "simulate" && <SimulationPanel profile={session.intake?.profile} />}
          {panelTab === "history" && <HistoryPanel userId={session.userId} followup={session.followup}
            refreshKey={`${session.threadId}:${session.turn}:${session.busy}`} />}
        </aside>}
      </div>
      <footer><span>仅为技术演示，不构成医疗建议。出现紧急不适，请立即拨打 <strong>120</strong>。</span><span>HealthPilot · 关心每一个小改变</span></footer>
    </main>
  </>;
}
