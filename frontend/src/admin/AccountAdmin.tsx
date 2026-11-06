import { useEffect, useState, type FormEvent } from "react";

type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type ManagedAccount = { id: string; username: string; role: "user" | "knowledge_admin" | "ops_admin"; created_at: number };
const ROLE_LABELS: Record<ManagedAccount["role"], string> = {
  user: "普通用户", knowledge_admin: "知识管理员", ops_admin: "运维管理员",
};

export function AccountAdmin({ api }: { api: Api }) {
  const [accounts, setAccounts] = useState<ManagedAccount[] | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<ManagedAccount["role"]>("user");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [resetFor, setResetFor] = useState("");
  const [nextPassword, setNextPassword] = useState("");
  const [nextAgain, setNextAgain] = useState("");
  const [draftRoles, setDraftRoles] = useState<Record<string, ManagedAccount["role"]>>({});

  function load(signal?: AbortSignal) {
    return api<{ accounts: ManagedAccount[] }>("/accounts", { signal })
      .then(body => { if (!signal?.aborted) setAccounts(body.accounts); });
  }

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal).catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [api]);

  async function create(event: FormEvent) {
    event.preventDefault();
    setError(""); setMessage("");
    if (password !== again) { setError("两次初始密码不一致。"); return; }
    setBusy(true);
    try {
      await api("/accounts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, role }) });
      setUsername(""); setPassword(""); setAgain(""); setRole("user");
      setMessage("账号已创建。请单独把初始密码交给使用者，本页不再显示。");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "账号未创建。");
    } finally { setBusy(false); }
  }

  async function saveRole(account: ManagedAccount, next: ManagedAccount["role"]) {
    setError(""); setMessage(""); setBusy(true);
    try {
      const result = await api<{ changed: boolean }>(`/accounts/${encodeURIComponent(account.username)}/role`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role: next }),
      });
      setMessage(result.changed ? "角色已更新，该账号的全部登录已退出。" : "角色未变化。");
      setDraftRoles(current => { const next = { ...current }; delete next[account.id]; return next; });
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "角色未更新。");
    } finally { setBusy(false); }
  }

  async function resetPassword(event: FormEvent, account: ManagedAccount) {
    event.preventDefault();
    setError(""); setMessage("");
    if (nextPassword !== nextAgain) { setError("两次新密码不一致。"); return; }
    setBusy(true);
    try {
      await api(`/accounts/${encodeURIComponent(account.username)}/password`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: nextPassword }),
      });
      setResetFor(""); setNextPassword(""); setNextAgain("");
      setMessage("密码已重置，该账号的全部登录已退出。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密码未重置。");
    } finally { setBusy(false); }
  }

  return <section className="admin-panel">
    <h2>账号</h2>
    <p>创建账号、调整角色或重置密码。密码只在提交时发送，不会留在页面提示或审计记录里。重置密码或变更角色后，该账号的全部登录都会退出。</p>
    {error && <p role="alert" className="admin-error">{error}</p>}
    {message && <p role="status">{message}</p>}
    <form className="admin-account-form" onSubmit={event => void create(event)}>
      <label htmlFor="account-username">用户名</label>
      <input id="account-username" autoComplete="off" required minLength={3} maxLength={64} value={username} disabled={busy} onChange={event => setUsername(event.target.value)} />
      <label htmlFor="account-role">角色</label>
      <select id="account-role" value={role} disabled={busy} onChange={event => setRole(event.target.value as ManagedAccount["role"])}>
        {Object.entries(ROLE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
      <label htmlFor="account-password">初始密码</label>
      <input id="account-password" type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={password} disabled={busy} onChange={event => setPassword(event.target.value)} />
      <label htmlFor="account-password-again">再次输入初始密码</label>
      <input id="account-password-again" type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={again} disabled={busy} onChange={event => setAgain(event.target.value)} />
      <button className="admin-primary" type="submit" disabled={busy}>{busy ? "正在保存…" : "创建账号"}</button>
    </form>
    {!accounts && !error && <p role="status">正在读取账号…</p>}
    {accounts?.length === 0 && <p>暂无账号。</p>}
    {accounts && accounts.length >= 500 && <p>最多显示 500 个账号。</p>}
    <ul className="admin-list">{accounts?.map(account => <li key={account.id}>
      <strong>{account.username}</strong> · {ROLE_LABELS[account.role]}
      <p className="admin-muted">创建于 {new Date(account.created_at * 1000).toLocaleString("zh-CN")}</p>
      <div className="admin-account-actions">
        <label>调整角色
          <select aria-label={`${account.username} 的角色`} value={draftRoles[account.id] ?? account.role} disabled={busy}
            onChange={event => setDraftRoles(current => ({ ...current, [account.id]: event.target.value as ManagedAccount["role"] }))}>
            {Object.entries(ROLE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <button type="button" disabled={busy || (draftRoles[account.id] ?? account.role) === account.role} onClick={() => void saveRole(account, draftRoles[account.id] ?? account.role)}>保存角色</button>
        <button type="button" disabled={busy} onClick={() => { setResetFor(account.username); setNextPassword(""); setNextAgain(""); setError(""); }}>重置密码</button>
      </div>
      {resetFor === account.username && <form className="admin-account-form" onSubmit={event => void resetPassword(event, account)}>
        <label htmlFor={`reset-${account.id}`}>新密码</label>
        <input id={`reset-${account.id}`} type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={nextPassword} disabled={busy} onChange={event => setNextPassword(event.target.value)} />
        <label htmlFor={`reset-again-${account.id}`}>再次输入新密码</label>
        <input id={`reset-again-${account.id}`} type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={nextAgain} disabled={busy} onChange={event => setNextAgain(event.target.value)} />
        <button className="admin-primary" type="submit" disabled={busy}>确认重置</button>
      </form>}
    </li>)}</ul>
  </section>;
}
