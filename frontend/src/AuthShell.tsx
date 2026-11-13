import { useEffect, useRef, useState, type FormEvent } from "react";
import { Workspace } from "./Workspace";
import { AuthContext } from "./AuthContext";
import { createAuthClient, onSessionExpired, type AuthUser } from "./auth";

const client = createAuthClient();
type State =
  | { phase: "checking" }
  | { phase: "signed_out"; notice?: string }
  | { phase: "unavailable"; notice: string }
  | { phase: "signed_in"; user: AuthUser };

export function AuthShell() {
  const [session, setSession] = useState<State>({ phase: "checking" });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);

  async function refresh() {
    const version = ++generation.current;
    try {
      const user = await client.me();
      if (version === generation.current) {
        setSession(user ? { phase: "signed_in", user } : { phase: "signed_out" });
      }
    } catch (problem) {
      if (version === generation.current) {
        setSession({
          phase: "unavailable",
          notice: problem instanceof Error ? problem.message : "Unable to verify session.",
        });
      }
    }
  }

  useEffect(() => {
    const unsubscribe = onSessionExpired(() => {
      ++generation.current;
      setSession({ phase: "signed_out", notice: "Your session expired. Sign in again." });
    });
    const focus = () => { if (document.visibilityState === "visible") void refresh(); };
    window.addEventListener("focus", focus);
    void refresh();
    return () => {
      ++generation.current;
      unsubscribe();
      window.removeEventListener("focus", focus);
    };
  }, []);

  async function signIn(username: string, password: string) {
    ++generation.current;
    setPending(true);
    setError("");
    try {
      const user = await client.login(username.trim(), password);
      setSession({ phase: "signed_in", user });
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Sign-in failed.");
    } finally {
      setPending(false);
    }
  }

  async function signOut() {
    ++generation.current;
    setPending(true);
    setError("");
    try {
      await client.logout();
      setSession({ phase: "signed_out", notice: "Signed out successfully." });
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Sign-out failed.");
    } finally {
      setPending(false);
    }
  }

  if (session.phase === "signed_in") {
    return (
      <AuthContext.Provider value={{ user: session.user, signOut, signingOut: pending }}>
        <Workspace />
        {error && <p className="auth-workspace-error" role="alert">{error}</p>}
      </AuthContext.Provider>
    );
  }
  return (
    <SignIn
      phase={session.phase}
      notice={session.phase === "checking" ? undefined : session.notice}
      pending={pending}
      error={error}
      onSubmit={signIn}
      retry={() => {
        setError("");
        setSession({ phase: "checking" });
        void refresh();
      }}
    />
  );
}

function SignIn({
  phase, notice, pending, error, onSubmit, retry,
}: {
  phase: State["phase"];
  notice?: string;
  pending: boolean;
  error: string;
  onSubmit: (username: string, password: string) => Promise<void>;
  retry: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!username.trim() || !password || pending) return;
    await onSubmit(username, password);
    setPassword("");
  }
  return (
    <div className="auth-layout">
      <main className="auth-card">
        <p className="auth-eyebrow">EXL · AGENTIC MODEL RISK FACTORY</p>
        <h1>Sign in to the workspace</h1>
        <p className="auth-intro">
          Access models, run history, evidence and human review using your assigned Backend account.
        </p>
        {phase === "checking" ? (
          <p role="status">Checking your session…</p>
        ) : (
          <>
            {notice && <p role={phase === "unavailable" ? "alert" : "status"} className="auth-notice">{notice}</p>}
            {phase === "unavailable" ? (
              <button type="button" className="button secondary" onClick={retry}>Retry connection</button>
            ) : (
              <form onSubmit={(event) => void submit(event)} className="auth-form">
                <label htmlFor="auth-username">Username</label>
                <input id="auth-username" name="username" autoComplete="username"
                  maxLength={100} required value={username}
                  onChange={(event) => setUsername(event.target.value)} />
                <label htmlFor="auth-password">Password</label>
                <input id="auth-password" name="password" type="password"
                  autoComplete="current-password" maxLength={256} required
                  value={password} onChange={(event) => setPassword(event.target.value)} />
                <button className="button primary" type="submit" disabled={pending}>
                  {pending ? "Signing in…" : "Sign in"}
                </button>
              </form>
            )}
            {error && <p role="alert" className="auth-notice">{error}</p>}
          </>
        )}
        <p className="auth-security-note">
          Sessions use an HTTP-only cookie. Do not paste API keys into this form.
          Your Backend administrator manages accounts and permissions.
        </p>
        <a href="/demo">View the separate sample demo</a>
      </main>
    </div>
  );
}
