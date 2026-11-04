import { useEffect, useState } from "react";
import type { AccessPolicy, AdminApi } from "./Approvals";

type Account = { id: string; username: string };

export function KnowledgePolicy({ api, initial, busy, onSubmit }: {
  api: AdminApi; initial: AccessPolicy; busy: boolean; onSubmit: (policy: AccessPolicy) => Promise<void>;
}) {
  const [policy, setPolicy] = useState(initial);
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void api<{ accounts: Account[] }>("/accounts", { signal: controller.signal })
      .then(body => { if (!controller.signal.aborted) setAccounts(body.accounts); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [api]);
  return <section className="admin-review" aria-label="文档访问范围">
    <h3>文档访问范围</h3><p>变更需生成确认单，核对后才会生效。限制为零个账号时，仅管理员可检索。</p>
    <label>允许访问<select disabled={busy} value={policy.visibility} onChange={event => setPolicy({ ...policy, visibility: event.target.value as AccessPolicy["visibility"] })}>
      <option value="shared">共享资料</option><option value="restricted">指定账号</option>
    </select></label>
    {policy.visibility === "restricted" && <>
      {!accounts && !error && <p role="status">正在读取账号…</p>}
      {accounts?.map(account => <label className="admin-account-option" key={account.id}><input type="checkbox" disabled={busy}
        checked={policy.allowed_user_ids.includes(account.id)} onChange={event => setPolicy({ ...policy,
          allowed_user_ids: event.target.checked ? [...policy.allowed_user_ids, account.id] : policy.allowed_user_ids.filter(id => id !== account.id) })} />{account.username}<small>{account.id}</small></label>)}
      {accounts && !accounts.length && <p>暂无账号。需先启用登录并创建账号。</p>}
    </>}
    {error && <p role="alert">{error}</p>}
    <button disabled={busy || !accounts} onClick={() => void onSubmit({ ...policy, allowed_user_ids: policy.visibility === "shared" ? [] : policy.allowed_user_ids })}>生成访问范围确认单</button>
  </section>;
}

type UsageGroup = { model: string; scene: string; status: string; calls: number; input_tokens: number | null; output_tokens: number | null; duration_ms: number; unknown_usage_calls: number };
const statuses: Record<string, string> = { succeeded: "成功", failed: "失败", timeout: "超时", cancelled: "取消", busy: "并发繁忙", access_changed: "资料权限已变化" };
export function ModelUsage({ api }: { api: AdminApi }) {
  const [groups, setGroups] = useState<UsageGroup[] | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api<{ groups: UsageGroup[] }>("/model-usage", { signal: controller.signal })
      .then(body => { if (!controller.signal.aborted) { setGroups(body.groups); setError(""); } })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [api, refresh]);
  return <section className="admin-panel"><h2>最近七天模型用量</h2>
    <p>按模型和场景汇总调用。Token 由提供商返回；缺失值不当作零。未接入价格表，不估算金额。模拟模式和向量模型不计入。</p>
    <button onClick={() => setRefresh(value => value + 1)}>刷新模型用量</button>
    {error && <p role="alert">{error}</p>}
    {!groups && !error && <p role="status">正在读取…</p>}
    {groups?.length === 0 && <p>暂无模型调用记录。</p>}
    <ul className="admin-list">{groups?.map(row => <li key={`${row.model}:${row.scene}:${row.status}`}><strong>{row.model}</strong> · {row.scene} · {statuses[row.status] ?? row.status}
      <p>{row.calls} 次调用 · 已知输入 Token {row.input_tokens ?? "未知"} · 已知输出 Token {row.output_tokens ?? "未知"} · 用量缺失 {row.unknown_usage_calls} 次</p>
      <p>累计耗时 {(row.duration_ms / 1000).toFixed(2)} 秒</p>
    </li>)}</ul>
  </section>;
}
