import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";

test("real provider retains context across follow-up, minimize and reload", async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.CHAT_LIVE_E2E !== "true",
    "Opt-in only: requires a real backend/provider and isolated test account.",
  );
  const evidence = {
    frontendCommit: process.env.CHAT_LIVE_FRONTEND_COMMIT ?? "not supplied",
    backendCommit: process.env.CHAT_LIVE_BACKEND_COMMIT ?? "not supplied",
    createdConversations: 0,
    turns: [] as {
      conversationId: string;
      turnId: string;
      clientMessageId: string;
      assistantMessageId: string;
    }[],
    recall: [] as boolean[],
  };
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/conversations"
    )
      evidence.createdConversations++;
  });
  const label = `orchard-${randomUUID()}`;
  try {
    await page.goto("/");
    const loginStatus = await page.evaluate(
      async (credentials) => {
        const response = await fetch("/api/v1/auth/login", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(credentials),
        });
        return response.status;
      },
      {
        username: process.env.CHAT_LIVE_USERNAME!,
        password: process.env.CHAT_LIVE_PASSWORD!,
      },
    );
    expect(loginStatus, "Test account must authenticate").toBe(200);
    await page.reload();
    await expect(
      page.getByRole("button", { name: "Sign out", exact: true }),
    ).toBeVisible();
    const open = () =>
      page
        .getByRole("button", { name: "Open agent chat", exact: true })
        .click();
    await open();
    // Fresh browser context and explicit new general conversation; no existing history reused.
    await page
      .getByRole("button", { name: "New conversation", exact: true })
      .click();
    async function send(prompt: string) {
      await page.getByLabel("Message the agent", { exact: true }).fill(prompt);
      const responsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          /\/api\/v1\/conversations\/[^/]+\/messages$/.test(
            new URL(response.url()).pathname,
          ),
      );
      await page.getByRole("button", { name: "Send", exact: true }).click();
      const response = await responsePromise;
      expect(
        response.status(),
        "Message must be acknowledged as a durable turn",
      ).toBe(202);
      const turn = await response.json();
      evidence.turns.push({
        conversationId: turn.conversation_id,
        turnId: turn.id,
        clientMessageId: turn.client_message_id,
        assistantMessageId: turn.assistant_message_id,
      });
      expect(new Set(evidence.turns.map((t) => t.conversationId)).size).toBe(1);
      const log = page.getByRole("log", { name: "Saved chat messages" });
      const replies = log.locator(".chat-message-assistant");
      await expect(replies).toHaveCount(evidence.turns.length, {
        timeout: 120_000,
      });
      const reply = replies.last();
      await expect(reply.locator("h3 span")).toHaveText("completed", {
        timeout: 120_000,
      });
      return (await reply.locator(".chat-text").innerText()).trim();
    }
    await send(
      `Remember this synthetic test label for this conversation: ${label}. Reply only OK. Do not call tools or perform any platform actions.`,
    );
    await page
      .getByRole("button", { name: "Minimize chat", exact: true })
      .click();
    await open();
    // The label is intentionally absent from follow-up prompts.
    evidence.recall.push(
      (
        await send(
          "What is the exact test label I gave you in my first message? Reply with only that label. Do not use tools.",
        )
      ).includes(label),
    );
    expect(
      evidence.recall[0],
      "Second reply must recall the synthetic label from prior history",
    ).toBe(true);
    await page.reload();
    await open();
    await expect(
      page
        .getByRole("log", { name: "Saved chat messages" })
        .locator(".chat-message-assistant"),
    ).toHaveCount(2);
    evidence.recall.push(
      (
        await send(
          "Repeat the exact test label from my first message once more. Reply with only that label. Do not use tools.",
        )
      ).includes(label),
    );
    expect(
      evidence.recall[1],
      "Reply after reload must recall the original label",
    ).toBe(true);
    expect(evidence.createdConversations).toBe(1);
    expect(new Set(evidence.turns.map((t) => t.turnId)).size).toBe(3);
    expect(new Set(evidence.turns.map((t) => t.clientMessageId)).size).toBe(3);
  } finally {
    // Sanitized tracing evidence only: no credentials, cookies, prompts or reply bodies.
    await testInfo.attach("chat-continuity-identifiers", {
      body: JSON.stringify(evidence, null, 2),
      contentType: "application/json",
    });
  }
});
