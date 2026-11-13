/** Cookie-session authentication. Never store credentials or tokens in browser storage. */
export type AuthRole = "admin" | "developer" | "validator" | "approver";
export type AuthUser = { id: string; role: AuthRole; username?: string; active?: boolean };
export type RunOperation = "document" | "rebuild" | "validate" | "monitor";

const validRoles: readonly string[] = ["admin", "developer", "validator", "approver"];
const expiredListeners = new Set<() => void>();
export function onSessionExpired(listener: () => void): () => void {
  expiredListeners.add(listener);
  return () => { expiredListeners.delete(listener); };
}
export function notifySessionExpired(): void {
  expiredListeners.forEach((listener) => listener());
}
export function canUpload(role: AuthRole): boolean {
  return role === "admin" || role === "developer";
}
export function canReview(role: AuthRole): boolean {
  return role === "validator" || role === "approver" || role === "admin";
}
export function canStartOperation(role: AuthRole, operation: RunOperation): boolean {
  if (operation === "document" || operation === "rebuild") return canUpload(role);
  if (operation === "validate") return role === "admin" || role === "validator";
  return role === "admin" || role === "developer" || role === "validator";
}

export class AuthError extends Error {
  constructor(public readonly status: number, message: string) { super(message); }
}
function readUser(value: unknown): AuthUser {
  if (!value || typeof value !== "object") throw new AuthError(200, "Invalid identity response.");
  const user = value as Record<string, unknown>;
  if (typeof user.id !== "string" || !user.id ||
      !validRoles.includes(String(user.role)) ||
      (user.username !== undefined && typeof user.username !== "string") ||
      (user.active !== undefined && typeof user.active !== "boolean")) {
    throw new AuthError(200, "Invalid identity response.");
  }
  return user as AuthUser;
}
function requestError(status: number, detail: unknown): AuthError {
  const defaults: Record<number, string> = {
    401: "Incorrect credentials or expired session.",
    403: "Access denied for this account.",
    429: "Too many sign-in attempts. Try again in five minutes.",
    503: "Authentication service is unavailable.",
  };
  return new AuthError(
    status,
    typeof detail === "string" ? detail : defaults[status] ?? "Authentication failed (HTTP " + status + ").",
  );
}
export function createAuthClient(baseUrl = "/api/v1", transport: typeof fetch = fetch) {
  const base = baseUrl.replace(/\/$/, "");
  async function request(path: string, init: RequestInit = {}): Promise<unknown> {
    let response: Response;
    try {
      response = await transport(base + path, {
        credentials: "include", cache: "no-store", ...init,
        headers: { "Content-Type": "application/json", ...init.headers },
      });
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") throw error;
      throw new AuthError(0, "Cannot reach the Backend. Check the connection and retry.");
    }
    let data: unknown;
    try { data = await response.json(); } catch { data = undefined; }
    if (!response.ok) {
      const detail = data && typeof data === "object" && "detail" in data
        ? (data as {detail: unknown}).detail : undefined;
      throw requestError(response.status, detail);
    }
    if (data === undefined) throw new AuthError(response.status, "Invalid authentication response.");
    return data;
  }
  return {
    async me(): Promise<AuthUser | null> {
      try { return readUser(await request("/auth/me")); }
      catch (error) {
        if (error instanceof AuthError && error.status === 401) return null;
        throw error;
      }
    },
    async login(username: string, password: string): Promise<AuthUser> {
      return readUser(await request("/auth/login", {
        method: "POST", body: JSON.stringify({ username, password }),
      }));
    },
    async logout(): Promise<void> {
      try { await request("/auth/logout", { method: "POST", body: "{}" }); }
      catch (error) {
        if (error instanceof AuthError && error.status === 401) return;
        throw error;
      }
    },
  };
}
