import { createContext, useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { setExpectedUser } from "../lib/api";

export type UserRole = "user" | "knowledge_admin" | "ops_admin" | "medical_reviewer" | "guest";
interface User { id: string; username: string; role: UserRole }
export interface GuestQuota { limit: number; remaining: number; hours: number; daily_available: boolean }
interface SessionInfo { required: boolean; user: User | null; expires_at?: number; guest?: GuestQuota | null }
export const AuthContext = createContext<{
  user: User | null; logout: () => void; busy: boolean; required: boolean;
  guest?: GuestQuota | null; refresh?: () => void; requestLogin?: () => void;
}>({ user: null, logout: () => {}, busy: false, required: false });

function notifyTabs() {
  try { localStorage.setItem("healthpilot-auth-change", crypto.randomUUID()); } catch { /* 存储不可用时由服务端校验会话。 */ }
}

function homePath(role: UserRole | undefined) {
  return role === "knowledge_admin" || role === "ops_admin" || role === "medical_reviewer" ? "/admin" : "/";
}

function normalizeRole(value: unknown): UserRole {
  return value === "knowledge_admin" || value === "ops_admin" || value === "medical_reviewer" || value === "user" || value === "guest" ? value : "user";
}

export function AuthGate({ children, adminPath = false }: { children: ReactNode; adminPath?: boolean }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [phase, setPhase] = useState<"checking" | "ready" | "login" | "unavailable">("checking");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // 服务端允许免登录体验时，首页未登录直接进入游客模式；登录页也提供入口
  const [guestOffered, setGuestOffered] = useState(false);
  // 游客点“登录”后暂存原会话，可返回继续体验
  const [guestSession, setGuestSession] = useState<SessionInfo | null>(null);
  const generation = useRef(0);
  const currentRole = useRef<UserRole | null>(null);
  const restart = useRef<() => void>(() => {});
  const expire = useCallback(() => {
    generation.current++;
    const wasGuest = currentRole.current === "guest";
    setExpectedUser(null); setSession(null); setGuestSession(null); setPassword(""); setBusy(false);
    // 游客会话到期后重新确认，可以开启新的体验
    if (wasGuest) { restart.current(); return; }
    setPhase("login"); setError("登录已失效或账号已切换，请重新登录。");
  }, []);

  const adopt = useCallback((body: SessionInfo) => {
    const role = normalizeRole(body.user?.role);
    const user = body.user ? { ...body.user, role } : null;
    const next = { ...body, user };
    const target = body.required && user ? homePath(role) : (adminPath ? "/admin" : "/");
    const onAdmin = window.location.pathname === "/admin";
    // / 与 /admin 是两套前端壳，必须整页切换；/login → / 只需替换地址。
    if ((target === "/admin") !== onAdmin) {
      window.location.assign(target);
      return false;
    }
    if (window.location.pathname !== target) window.history.replaceState(null, "", target);
    setExpectedUser(user?.id ?? null);
    currentRole.current = user?.role ?? null;
    setSession(next);
    setGuestSession(null);
    setPhase("ready");
    return true;
  }, [adminPath]);

  const startGuest = useCallback(async (current = ++generation.current) => {
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/v1/auth/guest", { method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "X-HealthPilot-Request": "1" } });
      const body = await response.json().catch(() => ({}));
      if (current !== generation.current) return;
      if (!response.ok) {
        setGuestOffered(false); setPhase("login");
        setError(typeof body.detail === "string" ? body.detail : "免登录体验暂不可用，请登录后使用。");
        return;
      }
      if (!body.user?.id || !body.expires_at) throw new Error();
      body.user.role = normalizeRole(body.user.role);
      adopt(body);
    } catch {
      if (current === generation.current) { setPhase("login"); setError("免登录体验暂不可用，请登录后使用。"); }
    } finally { if (current === generation.current) setBusy(false); }
  }, [adopt]);

  const check = useCallback(async (signal?: AbortSignal) => {
    const current = ++generation.current;
    setPhase("checking"); setExpectedUser(null); setSession(null); setError("");
    try {
      const response = await fetch("/api/v1/auth/me", { signal, credentials: "same-origin", cache: "no-store" });
      if (signal?.aborted || current !== generation.current) return;
      if (response.status === 401) {
        const offered = response.headers.get("X-HealthPilot-Guest") === "available";
        setGuestOffered(offered);
        if (offered && !adminPath && window.location.pathname !== "/login") { await startGuest(current); return; }
        setPhase("login"); return;
      }
      if (!response.ok) throw new Error();
      const body: SessionInfo = await response.json();
      if (signal?.aborted || current !== generation.current) return;
      if (typeof body.required !== "boolean" || (body.required && (!body.user?.id || !body.expires_at))) throw new Error();
      if (body.user) body.user.role = normalizeRole(body.user.role);
      if (body.user?.role === "guest" && (adminPath || window.location.pathname === "/login")) {
        // 游客打开登录页或后台时显示登录入口，保留游客会话以便返回
        setGuestOffered(true); setGuestSession(body); setPhase("login"); return;
      }
      adopt(body);
    } catch {
      if (!signal?.aborted && current === generation.current) {
        setPhase("unavailable"); setError("暂时无法确认登录状态，请检查网络后重试。");
      }
    }
  }, [adopt, adminPath, startGuest]);
  restart.current = () => { void check(); };

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/auth/me", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) return;
      const body: SessionInfo = await response.json();
      setSession(previous => previous && body.user?.id === previous.user?.id ? { ...previous, guest: body.guest ?? null } : previous);
    } catch { /* 额度显示稍后刷新，服务端仍会校验 */ }
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
    if (phase === "login" && !adminPath && window.location.pathname !== "/login") {
      window.history.replaceState(null, "", "/login");
    }
  }, [phase, adminPath]);

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
      body.user.role = normalizeRole(body.user.role);
      setPassword("");
      if (!adopt(body)) return;
      notifyTabs();
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
      if (adminPath) window.location.assign("/login");
    } catch { setError("退出未完成，请检查网络后再次退出。"); }
    finally { setBusy(false); }
  }

  const sessionRole = session?.user?.role;
  const adminSession = sessionRole === "knowledge_admin" || sessionRole === "ops_admin" || sessionRole === "medical_reviewer";

  if (phase === "ready" && session) {
    if (session.required && adminPath && !adminSession) {
      return <main className="auth-shell"><section className="auth-card" aria-label="权限提示">
        <a className="brand" href="/">✚ HealthPilot</a>
        <h1>无权访问管理后台</h1>
        <p>当前账号是普通用户，请返回健康工作台，或使用管理员账号登录。</p>
        <button type="button" onClick={() => window.location.assign("/")}>返回健康工作台</button>
        <button type="button" onClick={() => void logout()} disabled={busy}>退出登录</button>
      </section></main>;
    }
    const requestLogin = () => { setGuestSession(session); setPhase("login"); setError(""); };
    return <AuthContext.Provider value={{ user: session.user, logout: () => void logout(), busy, required: session.required,
      guest: session.guest ?? null, refresh: () => void refresh(), requestLogin }}>
      {error && <p className="auth-error" role="alert">{error}</p>}
      {!session.required && <div className="demo-notice">免登录演示 · 仅使用虚拟数据，演示身份不提供访问保护</div>}
      {children}
    </AuthContext.Provider>;
  }

  // /admin 在未登录时仍渲染后台，保留共享口令紧急入口与现有测试路径。
  if (adminPath && (phase === "login" || phase === "checking" || phase === "unavailable")) {
    return <AuthContext.Provider value={{ user: null, logout: () => {}, busy, required: true }}>
      {phase === "unavailable" && error && <p className="auth-error" role="alert">{error}</p>}
      {children}
    </AuthContext.Provider>;
  }

  return <main className="auth-shell"><section className="auth-card" aria-label="账号登录">
    <a className="brand" href="/">✚ HealthPilot</a>
    <h1>登录健康助手</h1><p>使用管理员为你创建的账号。普通用户进入健康评估；知识、医学审核或运维管理员进入对应后台。</p>
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
    {phase === "login" && !adminPath && (guestSession
      ? <button type="button" className="quiet" disabled={busy} onClick={() => { if (guestSession) adopt(guestSession); }}>继续免登录体验</button>
      : guestOffered && <button type="button" className="quiet" disabled={busy} onClick={() => void startGuest()}>免登录体验</button>)}
    <p className="auth-note">技术演示，不构成医疗建议。账号由管理员开通；登录后可自行修改密码。</p>
  </section></main>;
}
