let expectedUser: string | null = null;

export function setExpectedUser(id: string | null) { expectedUser = id; }

export function sessionExpired() {
  window.dispatchEvent(new Event("healthpilot-session-expired"));
}

export async function apiFetch(input: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set("X-HealthPilot-Request", "1");
  if (expectedUser) headers.set("X-HealthPilot-User", expectedUser);
  const response = await fetch(input, { ...init, headers, credentials: "same-origin", cache: "no-store" });
  if (expectedUser && response.status === 401) sessionExpired();
  if (expectedUser && response.status === 403) {
    const body = await response.clone().json().catch(() => ({}));
    if (body.code === "SESSION_CHANGED") sessionExpired();
  }
  return response;
}
