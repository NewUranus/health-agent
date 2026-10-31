import { useEffect, useRef, type CSSProperties } from "react";
import type { NodeEvent, TraceTurn } from "../types";
import { agents, fieldNames, nodeLabels } from "../lib/labels";

function TraceCard({ event, turn, highlighted }: { event: NodeEvent; turn: number; highlighted: string | null }) {
  const data = event.data ?? {};
  const [owner, color] = agents[event.agent] ?? [event.agent, "#80988b"];
  return (
    <article className="trace-card" style={{ "--agent-color": color } as CSSProperties}>
      <div className="trace-title"><strong>{nodeLabels[event.node] ?? event.node}</strong><span className="trace-time">{event.ms} ms</span></div>
      <span className="agent-tag">{owner}</span>
      {event.node === "intake" && <>
        <p>{data.missing?.length ? `还需补充：${fieldNames(data.missing)}` : "必填资料已齐全"}</p>
        {!!data.from_memory?.length && <p className="success">来自长期记忆：{fieldNames(data.from_memory)}</p>}
      </>}
      {event.node === "safety_check" && <p className={data.red_flags?.length ? "warning" : "success"}>
        {data.red_flags?.length ? "发现急症信号，立即进入紧急指引。" : "未命中急症关键词，继续问诊。"}
      </p>}
      {event.node === "supervisor" && <p>{agents[data.route ?? ""]?.[0] ?? data.route} · {data.reason}</p>}
      {event.node === "verify" && <p className={data.ok ? "success" : "warning"}>
        {data.ok ? `引用校验通过 · 第 ${data.attempt} 次` : (data.problems ?? []).join("；")}
      </p>}
      {data.followup && <p>{data.followup}</p>}
      {data.degraded && <p className="warning">模型暂不可用，已降级为本地模板。</p>}
      {data.fallback && <p className="warning">生成内容未通过校验，已使用安全模板。</p>}
      {data.prompt_version && <p>提示词版本 {data.prompt_version}</p>}
      {(data.tool_calls ?? []).map((call, index) => <details key={index}>
        <summary>{call.tool} · MCP{call.cached ? " · 缓存命中" : ""}</summary>
        <pre>{JSON.stringify({ 参数: call.args, 结果: call.result }, null, 2)}</pre>
      </details>)}
      {(data.chunks ?? []).map((chunk, index) => <div key={`${chunk.id}:${index}`} tabIndex={-1}
        className={`source-card${highlighted === `${turn}:${chunk.id}` ? " highlight" : ""}`}>
        <b>{chunk.title}</b><p>{chunk.source}</p><p>相关度 {Number(chunk.score).toFixed(3)}</p>
      </div>)}
      {event.node === "checkin" && <p>风险变化：{(data.delta ?? 0) > 0 ? "+" : ""}{data.delta} 个百分点</p>}
    </article>
  );
}

export function TracePanel({ turns, highlighted, selection }: {
  turns: TraceTurn[];
  highlighted: string | null;
  selection: number;
}) {
  const traces = useRef<HTMLDivElement>(null);
  const count = turns.reduce((total, turn) => total + turn.nodes.length, 0);
  useEffect(() => {
    if (traces.current) traces.current.scrollTop = traces.current.scrollHeight;
  }, [count, turns.length]);
  useEffect(() => {
    const sources = traces.current?.querySelectorAll<HTMLElement>(".source-card.highlight");
    const source = sources?.[sources.length - 1];
    source?.scrollIntoView({ behavior: "smooth", block: "center" });
    source?.focus({ preventScroll: true });
  }, [highlighted, selection]);

  return <aside className="trace-panel" id="trace-panel" aria-label="Agent 轨迹">
    <div className="panel-heading"><h2>Agent 轨迹 <span>{count}</span></h2><span className="live-label">实时</span></div>
    <p className="trace-intro">从信息收集到建议生成，查看每一步的依据。</p>
    <div className="traces" ref={traces}>
      {!turns.length && <div className="trace-empty"><span aria-hidden="true">⌘</span><h3>看见推理的过程</h3><p>发送一条消息后，这里将显示路由、工具结果和参考来源。</p></div>}
      {turns.map(({ turn, nodes }) => <div key={turn}>
        <div className="turn-marker">第 {turn} 轮对话</div>
        {nodes.map((event, index) => <TraceCard key={index} event={event} turn={turn} highlighted={highlighted} />)}
      </div>)}
    </div>
  </aside>;
}
