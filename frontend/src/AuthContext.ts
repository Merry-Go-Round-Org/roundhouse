import { createContext, useContext } from "react";
import type { AuthUser } from "./auth";

export type AuthSession = {
  user: AuthUser;
  signOut: () => Promise<void>;
  signingOut: boolean;
};

export const AuthContext = createContext<AuthSession | null>(null);

export function useAuth(): AuthSession {
  const value = useContext(AuthContext);
  if (!value) throw new Error("Live workspace requires an authenticated session.");
  return value;
}
