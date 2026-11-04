import { createContext, useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { setExpectedUser } from "../lib/api";

interface User { id: string; username: string }
interface SessionInfo { required: boolean; user: User | null; expires_at?: number }
export const AuthContext = createContext<{ user: User | null; logout: () => void; busy: boolean }>({ user: null, logout: () => {}, busy: false });

function notifyTabs() {
  try { localStorage.setItem("healthpilot-auth-change", crypto.randomUUID()); } catch { /* 存储不可用时由服务端校验会话。 */ }
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [phase, setPhase] = useState<"checking" | "ready" | "login" | "unavailable">("checking");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const expire = useCallback(() => {
    generation.current++;
    setExpectedUser(null); setSession(null); setPassword(""); setBusy(false);
    setPhase("login"); setError("登录已失效或账号已切换，请重新登录。");
  }, []);

  const check = useCallback(async (signal?: AbortSignal) => {
    const current = ++generation.current;
    setPhase("checking"); setExpectedUser(null); setSession(null); setError("");
    try {
      const response = await fetch("/api/v1/auth/me", { signal, credentials: "same-origin", cache: "no-store" });
      if (signal?.aborted || current !== generation.current) return;
      if (response.status === 401) { setPhase("login"); return; }
      if (!response.ok) throw new Error();
      const body: SessionInfo = await response.json();
      if (signal?.aborted || current !== generation.current) return;
      if (typeof body.required !== "boolean" || (body.required && (!body.user?.id || !body.expires_at))) throw new Error();
      setExpectedUser(body.user?.id ?? null); setSession(body); setPhase("ready");
    } catch {
      if (!signal?.aborted && current === generation.current) {
        setPhase("unavailable"); setError("暂时无法确认登录状态，请检查网络后重试。");
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void check(controller.signal);
    const storage = (event: StorageEvent) => { if (event.key === "healthpilot-auth-change") expire(); };
    const restore = (event: PageTransitionEvent) => { if (event.persisted) void check(); };
    const navigate = () => { void check(); };
    window.addEventListener("storage", storage);
    window.addEventListener("pageshow", restore);
    window.addEventListener("popstate", navigate);
    window.addEventListener("healthpilot-session-expired", expire);
    return () => {
      controller.abort(); generation.current++; setExpectedUser(null);
      window.removeEventListener("storage", storage);
      window.removeEventListener("pageshow", restore);
      window.removeEventListener("popstate", navigate);
      window.removeEventListener("healthpilot-session-expired", expire);
    };
  }, [check, expire]);

  useEffect(() => {
    // 登录页与健康首页使用独立地址，替换记录避免返回已失效的页面。
    const path = phase === "login" ? "/login" : phase === "ready" ? "/" : null;
    if (path && window.location.pathname !== path) window.history.replaceState(null, "", path);
  }, [phase]);

  useEffect(() => {
    if (!session?.expires_at) return;
    const timeout = window.setTimeout(expire, Math.max(0, session.expires_at * 1000 - Date.now()));
    return () => window.clearTimeout(timeout);
  }, [session?.expires_at, expire]);

  async function login(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const current = ++generation.current;
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/v1/auth/login", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-HealthPilot-Request": "1" },
        body: JSON.stringify({ username, password }) });
      const body = await response.json();
      if (current !== generation.current) return;
      if (!response.ok) throw new Error(body.detail ?? "登录失败，请稍后重试。");
      if (!body.user?.id || !body.expires_at) throw new Error("登录响应无效，请稍后重试。");
      setExpectedUser(body.user.id); setSession(body); setPassword(""); setPhase("ready"); notifyTabs();
    } catch (reason) {
      if (current === generation.current) setError(reason instanceof Error ? reason.message : "网络连接失败，请稍后重试。");
    } finally { if (current === generation.current) setBusy(false); }
  }

  async function logout() {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin",
        headers: { "X-HealthPilot-Request": "1" } });
      if (!response.ok) throw new Error();
      generation.current++; setExpectedUser(null); setSession(null); setPassword(""); setPhase("login"); notifyTabs();
    } catch { setError("退出未完成，请检查网络后再次退出。"); }
    finally { setBusy(false); }
  }

  if (phase === "ready" && session) return <AuthContext.Provider value={{ user: session.user, logout: () => void logout(), busy }}>
    {error && <p className="auth-error" role="alert">{error}</p>}
    {!session.required && <div className="demo-notice">免登录演示 · 仅使用虚拟数据，演示身份不提供访问保护</div>}
    {children}
  </AuthContext.Provider>;

  return <main className="auth-shell"><section className="auth-card" aria-label="账号登录">
    <a className="brand" href="/">✚ HealthPilot</a>
    <h1>登录健康助手</h1><p>使用管理员为你创建的账号，访问自己的健康会话和随访记录。</p>
    {error && <p className="auth-error" role="alert">{error}</p>}
    {phase === "checking" ? <p role="status">正在确认登录状态…</p> : phase === "unavailable"
      ? <button type="button" onClick={() => void check()}>重新连接</button>
      : <form onSubmit={(event) => void login(event)}>
        <label htmlFor="login-username">账号</label>
        <input id="login-username" autoComplete="username" maxLength={64} required value={username} onChange={e => setUsername(e.target.value)} disabled={busy} />
        <label htmlFor="login-password">密码</label>
        <input id="login-password" type="password" autoComplete="current-password" maxLength={256} required value={password} onChange={e => setPassword(e.target.value)} disabled={busy} />
        <button type="submit" disabled={busy}>{busy ? "正在登录…" : "登录"}</button>
      </form>}
    <p className="auth-note">技术演示，不构成医疗建议。账号开通或密码重置请联系管理员。</p>
  </section></main>;
}
