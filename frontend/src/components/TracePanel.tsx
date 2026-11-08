import { useEffect, useRef, type CSSProperties } from "react";
import type { NodeData, NodeEvent, TraceTurn } from "../types";
import { adherenceLabels, agents, fieldNames, nodeLabels } from "../lib/labels";

function safeUrl(value?: string): string | undefined {
  try {
    const url = new URL(value ?? "");
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

function safetySummary(data: NodeData): string {
  if (data.red_flags?.length) {
    return data.source === "model" && data.evidence
      ? `模型识别出急症信号：「${data.evidence}」，立即进入紧急指引。`
      : "发现急症信号，立即进入紧急指引。";
  }
  if (data.classifier_failed) return "未命中急症关键词；模型分诊未完成，已按规则结果继续。";
  if (data.classifier === "ok") return "规则与模型均未发现急症信号，继续问诊。";
  return "未命中急症关键词，继续问诊。";
}

function GroundingNote({ data }: { data: NodeData }) {
  if (data.grounding_skipped) {
    const reason = data.grounding_skip_reason;
    if (reason === "mock") return <p>事实校验：Mock 模式未执行。</p>;
    if (reason === "template") return <p className="success">事实校验：内容逐字摘自资料，无需判定。</p>;
    return <p className="warning">事实校验未完成（判定模型不可用），回答未经逐句核对。</p>;
  }
  const grounding = data.grounding;
  if (!grounding) return null;
  const failed = grounding.unsupported.length;
  if (failed && !grounding.removed) return <p className="warning">事实校验：{failed} 句在资料中找不到依据，已要求重写。</p>;
  return <>
    <p className={grounding.removed ? "warning" : "success"}>
      事实校验：{grounding.checked - failed} 句通过，删除 {grounding.removed} 句{grounding.rewritten ? "（已重写一次）" : ""}
    </p>
    {data.grounding_refused && <p className="warning">删除后没有可用内容，已改为拒答。</p>}
    {data.grounding_fallback && <p className="warning">删除后建议不完整，已改用资料原文。</p>}
  </>;
}

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
      {event.node === "safety_check" && <p className={data.red_flags?.length || data.classifier_failed ? "warning" : "success"}>
        {safetySummary(data)}
      </p>}
      {event.node === "supervisor" && <p>{agents[data.route ?? ""]?.[0] ?? data.route} · {data.reason}</p>}
      {event.node === "verify" && <p className={data.ok ? "success" : "warning"}>
        {data.ok ? `引用校验通过 · 第 ${data.attempt} 次` : (data.problems ?? []).join("；")}
      </p>}
      {(event.node === "answer" || event.node === "verify") && <GroundingNote data={data} />}
      {data.followup && <p>{data.followup}</p>}
      {data.degraded && <p className="warning">{data.web_search ? "回答模型暂不可用，请在来源卡片查看原文。" : "模型暂不可用，已降级为本地模板。"}</p>}
      {data.no_evidence && <p className="warning">没有足够依据，已拒答。</p>}
      {event.node === "web_retrieve" && <>
        <p className={data.web_failed ? "warning" : "success"}>
          {data.web_failed ? "没有取得可用网页正文。" : `已读取 ${data.web_read_count ?? 0} 个来源，选取 ${data.chunks?.length ?? 0} 个片段。`}
        </p>
        {[...new Set(data.web_errors ?? [])].map((error, index) => <p className="warning" key={index}>{error}</p>)}
        {!!data.web_results?.length && <details><summary>搜索结果与请求链接（不代表已读取）</summary>
          {data.web_results.map((item, index) => <p key={index}>{safeUrl(item.url)
            ? <a href={safeUrl(item.url)} target="_blank" rel="noopener noreferrer">{item.title || item.url}</a> : "网址格式无效"}</p>)}
        </details>}
      </>}
      {data.web_sources_only && <p>当前仅展示资料来源，未使用模型生成健康建议。</p>}
      {data.fallback && !data.no_evidence && <p className="warning">生成内容未通过校验，已使用安全模板。</p>}
      {data.prompt_version && <p>提示词版本 {data.prompt_version}</p>}
      {data.evidence_unavailable && <p className="warning">此步骤使用的资料已撤回或权限已变化，相关内容已隐藏。</p>}
      {(data.tool_calls ?? []).map((call, index) => <details key={index}>
        <summary>{call.tool} · MCP{call.cached ? " · 缓存命中" : ""}</summary>
        <pre>{JSON.stringify({ 参数: call.args, 结果: call.result }, null, 2)}</pre>
      </details>)}
      {(data.chunks ?? []).map((chunk, index) => <div key={`${chunk.id}:${index}`} tabIndex={-1}
        className={`source-card${highlighted === `${turn}:${chunk.id}` ? " highlight" : ""}`}>
        <b>{chunk.title}</b><p>{chunk.source}</p>{chunk.document_id != null && <p>文档 {chunk.document_id} · {chunk.version || "未标版本"} · 修订 {chunk.revision ?? 1}</p>}
        {chunk.document_id != null && <p className={chunk.reviewed_by ? "success" : "warning"}>{chunk.reviewed_by ? `已由 ${chunk.reviewed_by} 核对${chunk.effective_date ? ` · 生效日期 ${chunk.effective_date}` : ""}` : "该依据尚未经人工核对"}</p>}
        {chunk.page != null && <p>第 {chunk.page} 页</p>}<p>相关度 {Number(chunk.score).toFixed(3)}</p>
        {chunk.text && <details open={highlighted === `${turn}:${chunk.id}`}><summary>查看依据原文</summary><p className="source-text">{chunk.text}</p></details>}
        {chunk.origin === "web" && <>
          <p className="warning">网络资料 · 未经医学审核</p>
          {chunk.fetched_at && <p>获取时间：{new Date(chunk.fetched_at).toLocaleString("zh-CN")}</p>}
          {safeUrl(chunk.url) && <a href={safeUrl(chunk.url)} target="_blank" rel="noopener noreferrer">打开原文 ↗</a>}
        </>}
      </div>)}
      {event.node === "checkin_question" && <p>第 {data.step}/{data.total} 题</p>}
      {event.node === "checkin" && <>
        {!!data.red_flags?.length && <p className="warning">发现急症信号，随访已中止。{data.source === "model" && data.evidence ? `模型依据：「${data.evidence}」` : ""}</p>}
        {data.cancelled && <p>已取消本次随访，记录没有变化。</p>}
        {typeof data.delta === "number" && <p>风险变化：{data.delta > 0 ? "+" : ""}{data.delta} 个百分点</p>}
        {data.adherence && <p>依从情况：{adherenceLabels[data.adherence] ?? "未明确"}</p>}
      </>}
      {data.due_date && <p>下次复查日期：{data.due_date}</p>}
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

  return <>
    <p className="trace-intro">从信息收集到建议生成，查看每一步的依据。</p>
    <div className="traces" ref={traces}>
      {!turns.length && <div className="trace-empty"><span aria-hidden="true">⌘</span><h3>看见推理的过程</h3><p>发送一条消息后，这里将显示路由、工具结果和参考来源。</p></div>}
      {turns.map(({ turn, nodes }) => <div key={turn}>
        <div className="turn-marker">第 {turn} 轮对话</div>
        {nodes.map((event, index) => <TraceCard key={index} event={event} turn={turn} highlighted={highlighted} />)}
      </div>)}
    </div>
  </>;
}
