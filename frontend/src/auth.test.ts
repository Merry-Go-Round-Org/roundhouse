import { describe, expect, it, vi } from "vitest";
import {
  AuthError, canReview, canStartOperation, canUpload,
  createAuthClient, notifySessionExpired, onSessionExpired,
} from "./auth";

describe("session cookie contract", () => {
  const account = { id: "user-1", username: "tester", role: "developer", active: true };
  it("uses credentials and no-store when restoring a session", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(account));
    expect(await createAuthClient("/api/v1", fetcher).me()).toEqual(account);
    expect(fetcher).toHaveBeenCalledWith(
      "/api/v1/auth/me",
      expect.objectContaining({ credentials: "include", cache: "no-store" }),
    );
  });
  it("treats an unauthenticated session as signed out", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 401 }));
    await expect(createAuthClient("/api/v1", fetcher).me()).resolves.toBeNull();
  });
  it("signs in and signs out through the cookie API", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(account))
      .mockResolvedValueOnce(Response.json({ status: "signed_out" }));
    const auth = createAuthClient("/api/v1", fetcher);
    expect(await auth.login("tester", "private-password")).toEqual(account);
    await auth.logout();
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/auth/login", "/api/v1/auth/logout",
    ]);
    expect(fetcher.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: "POST", credentials: "include",
      body: JSON.stringify({ username: "tester", password: "private-password" }),
    }));
  });
  it("distinguishes rate limits, outages and invalid identities", async () => {
    const limited = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 429 }));
    await expect(createAuthClient("/api/v1", limited).login("u", "p"))
      .rejects.toMatchObject({ status: 429 });
    const offline = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("offline"));
    await expect(createAuthClient("/api/v1", offline).me())
      .rejects.toMatchObject({ status: 0 });
    const malformed = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ id: "a", role: "root" }));
    await expect(createAuthClient("/api/v1", malformed).me())
      .rejects.toBeInstanceOf(AuthError);
  });
  it("delivers 401 notifications only while subscribed", () => {
    const listener = vi.fn();
    const unsubscribe = onSessionExpired(listener);
    notifySessionExpired();
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
    notifySessionExpired();
    expect(listener).toHaveBeenCalledTimes(1);
  });
});

describe("role controls mirror Backend operation policy", () => {
  it("developers can upload and document, but cannot validate or approve", () => {
    expect(canUpload("developer")).toBe(true);
    expect(canStartOperation("developer", "document")).toBe(true);
    expect(canStartOperation("developer", "validate")).toBe(false);
    expect(canReview("developer")).toBe(false);
  });
  it("validator, approver and admin permissions stay distinct", () => {
    expect(canReview("validator")).toBe(true);
    expect(canStartOperation("validator", "validate")).toBe(true);
    expect(canUpload("validator")).toBe(false);
    expect(canReview("approver")).toBe(true);
    expect(canStartOperation("approver", "document")).toBe(false);
    expect(canUpload("admin")).toBe(true);
    expect(canStartOperation("admin", "validate")).toBe(true);
    expect(canReview("admin")).toBe(true);
  });
});
