import type { NodeEvent } from "../types";
import { nodeLabels } from "../lib/labels";

export function ExecutionProgress({ nodes, busy, status }: { nodes: NodeEvent[]; busy: boolean; status: string }) {
  if (!nodes.length && !busy) return null;
  return <details className="execution-progress" open={busy}><summary>执行进度 · 已完成 {nodes.length} 个步骤 · 节点累计 {(nodes.reduce((sum, node) => sum + node.ms, 0) / 1000).toFixed(1)} 秒</summary>
    <ol>{nodes.map((node, index) => <li key={index}><span>✓ {nodeLabels[node.node] ?? node.node}</span><small>{node.ms} ms</small></li>)}
      {busy && <li aria-current="step">{status}</li>}</ol>
    {busy && <small>剩余时间暂无法可靠预估。</small>}
  </details>;
}
