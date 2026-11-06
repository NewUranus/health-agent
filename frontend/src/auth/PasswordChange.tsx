import { useState, type FormEvent } from "react";
import { apiFetch } from "../lib/api";

export function PasswordChange() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function close() {
    setOpen(false); setCurrent(""); setNext(""); setAgain(""); setError(""); setMessage("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(""); setMessage("");
    if (next !== again) { setError("两次新密码不一致。"); return; }
    setBusy(true);
    try {
      const response = await apiFetch("/api/v1/auth/password", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "密码未修改。");
      setCurrent(""); setNext(""); setAgain("");
      setMessage("密码已更新，其他设备上的登录已退出。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密码未修改。");
    } finally { setBusy(false); }
  }

  return <>
    <button type="button" className="quiet" onClick={() => setOpen(true)}>修改密码</button>
    {open && <div className="password-backdrop">
      <form className="auth-card" role="dialog" aria-modal="true" aria-labelledby="password-title" onSubmit={event => void submit(event)}>
        <h2 id="password-title">修改密码</h2>
        <p>新密码至少 12 个字符。保存后，其他设备上的登录会退出，当前页面保持登录。</p>
        {error && <p className="auth-error" role="alert">{error}</p>}
        {message && <p role="status">{message}</p>}
        <label htmlFor="current-password">当前密码</label>
        <input id="current-password" type="password" autoComplete="current-password" required maxLength={256} value={current} onChange={event => setCurrent(event.target.value)} disabled={busy} />
        <label htmlFor="new-password">新密码</label>
        <input id="new-password" type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={next} onChange={event => setNext(event.target.value)} disabled={busy} />
        <label htmlFor="confirm-password">再次输入新密码</label>
        <input id="confirm-password" type="password" autoComplete="new-password" required minLength={12} maxLength={256} value={again} onChange={event => setAgain(event.target.value)} disabled={busy} />
        <button type="submit" disabled={busy}>{busy ? "正在保存…" : "保存新密码"}</button>
        <button type="button" onClick={close} disabled={busy}>关闭</button>
      </form>
    </div>}
  </>;
}
