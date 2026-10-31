import { useEffect, useReducer, useRef, useState } from "react";
import { HealthOverview } from "./components/HealthOverview";
import { MessageContent } from "./components/MessageContent";
import { TracePanel } from "./components/TracePanel";
import { createSseParser } from "./lib/sse";
import { createId, initialSession, saveUserId, sessionReducer } from "./lib/session";

const examples = [
  ["开始一次风险评估 ↗", "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药"],
  ["模拟降低血压", "如果我把血压降到120，风险能降多少？"],
  ["记录一次复查", "我来复查，最近血压130/85"],
  ["急症护栏演示", "突然胸口剧痛，出冷汗"],
];

export default function App() {
  const [session, dispatch] = useReducer(sessionReducer, undefined, initialSession);
  const [mode, setMode] = useState("正在连接");
  const [input, setInput] = useState("");
  const [traceVisible, setTraceVisible] = useState(true);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const [selection, setSelection] = useState(0);
  const messages = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const activeRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/health", { signal: controller.signal })
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
    if (messages.current) messages.current.scrollTop = messages.current.scrollHeight;
  }, [session.messages]);

  async function send(text: string) {
    text = text.trim();
    if (activeRequest.current || session.busy || !text) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    dispatch({ type: "send", text });
    setInput("");
    let gotDone = false;
    let gotMessage = false;
    let gotError = false;
    let status = "连接未完成";
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const response = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ thread_id: session.threadId, user_id: session.userId, message: text }),
      });
      if (!response.ok || !response.body) throw new Error("连接暂不可用，请稍后重试。");
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = createSseParser((event) => {
        if (controller.signal.aborted) return;
        if (event.type === "done") gotDone = true;
        if (event.type === "message") gotMessage = true;
        if (event.type === "error") gotError = true;
        dispatch({ type: "event", event });
      });
      while (true) {
        const { value, done } = await reader.read();
        parser.push(decoder.decode(value, { stream: !done }));
        if (done) { parser.flush(); break; }
      }
      if (!gotError && (!gotDone || !gotMessage)) throw new Error("连接中断，回复未完成。请重试。");
      status = gotError ? "本轮未完成，请检查信息后重试" : "本轮已完成 · 点击引用查看来源";
    } catch (error) {
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
        dispatch({ type: "finish", status });
        textarea.current?.focus();
      }
    }
  }

  function reset(clearIdentity = false) {
    if (activeRequest.current || session.busy) return;
    const userId = clearIdentity ? createId() : undefined;
    if (userId) saveUserId(userId);
    dispatch({ type: "reset", threadId: createId(), userId });
    setInput("");
    setHighlighted(null);
  }

  function selectCitation(turn: number, id: string) {
    setTraceVisible(true);
    setHighlighted(`${turn}:${id}`);
    setSelection((previous) => previous + 1);
  }

  return <>
    <header className="topbar">
      <a className="brand" href="/" aria-label="HealthPilot 首页"><span className="brand-icon" aria-hidden="true">✚</span><span>HealthPilot<small>心血管健康助手</small></span></a>
      <div className="header-actions">
        <span className="mode">{mode}</span>
        <button type="button" className="quiet" disabled={session.busy} onClick={() => reset()}>新会话 <span aria-hidden="true">＋</span></button>
        <button type="button" className="quiet" disabled={session.busy} onClick={() => reset(true)}>清除身份</button>
      </div>
    </header>
    <main>
      <HealthOverview intake={session.intake} risk={session.risk} placeholder={session.profilePlaceholder} status={session.profileStatus} />
      <div className={`workspace${traceVisible ? "" : " trace-hidden"}`}>
        <section className="chat-panel" aria-label="健康对话">
          <div className="panel-heading"><h2><span className="status-dot" /> 健康对话</h2>
            <button type="button" className="quiet" aria-expanded={traceVisible} aria-controls="trace-panel" onClick={() => setTraceVisible(!traceVisible)}>
              {traceVisible ? "收起轨迹 ↗" : "展开轨迹 ↙"}
            </button>
          </div>
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
            <div className="examples" aria-label="试试这些示例">
              {examples.map(([label, text]) => <button type="button" key={label} disabled={session.busy} onClick={() => void send(text)}>{label}</button>)}
            </div>
            <form id="chat-form" onSubmit={(event) => { event.preventDefault(); void send(input); }}>
              <label htmlFor="message-input" className="sr-only">输入健康信息或问题</label>
              <textarea id="message-input" ref={textarea} rows={2} maxLength={12000} required
                placeholder="告诉我你的健康情况，或问一个健康问题…" value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                    event.preventDefault(); void send(input);
                  }
                }} />
              <button className="send" type="submit" disabled={session.busy} aria-label={session.busy ? "处理中…" : "发送消息"}>
                {session.busy ? "处理中…" : <>发送 <span aria-hidden="true">↑</span></>}
              </button>
            </form>
            <div className="composer-footer"><span role="status">{session.status}</span><span>Enter 发送 · Shift + Enter 换行</span></div>
          </div>
        </section>
        {traceVisible && <TracePanel turns={session.traces} highlighted={highlighted} selection={selection} />}
      </div>
      <footer><span>仅为技术演示，不构成医疗建议。出现紧急不适，请立即拨打 <strong>120</strong>。</span><span>HealthPilot · 关心每一个小改变</span></footer>
    </main>
  </>;
}
