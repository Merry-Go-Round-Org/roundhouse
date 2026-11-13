import { test, expect } from "@playwright/test";
import type { ChatMessage, ChatTurn } from "../src/durableChat";

// Issue #18: distinguish browser conversation identity from LLM memory.
// These fixtures verify requests and saved history, not real harness continuity.
for (const modelId of [null, "m1"]) {
  test(`follow-up messages retain ${modelId ? "model" : "general"} conversation across navigation and reload`, async ({
    page,
  }) => {
    const date = "2026-11-08T12:00:00Z";
    const model = {
      id: "m1",
      name: "Credit",
      version: "1",
      language: "R",
      dataset_id: "uci_credit",
      purpose: "Demo",
      owner: null,
      lifecycle_status: "development",
      created_at: date,
    };
    const conversations = new Map<
      string,
      { messages: ChatMessage[]; turns: ChatTurn[] }
    >();
    const submissions: {
      conversation: string;
      clientId: string;
      modelId: string | null;
    }[] = [];
    let created = 0;
    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const post = request.method() === "POST";
      if (path.endsWith("/auth/me"))
        return route.fulfill({
          json: {
            id: "continuity-user",
            username: "operator",
            role: "developer",
            active: true,
          },
        });
      if (path === "/api/v1/models") return route.fulfill({ json: [model] });
      if (path === "/api/v1/models/m1") return route.fulfill({ json: model });
      if (path === "/api/v1/conversations" && post) {
        const id = `c${++created}`;
        conversations.set(id, { messages: [], turns: [] });
        return route.fulfill({ status: 201, json: { id } });
      }
      const match = path.match(/^\/api\/v1\/conversations\/([^/]+)\/(.*)$/);
      if (!match) return route.fulfill({ json: [] });
      const [, id, resource] = match;
      const saved = conversations.get(id);
      if (!saved)
        return route.fulfill({
          status: 404,
          json: { detail: "Unknown conversation" },
        });
      if (resource === "messages" && post) {
        const body = request.postDataJSON();
        submissions.push({
          conversation: id,
          clientId: body.client_message_id,
          modelId: body.context.model_id,
        });
        const index = saved.turns.length + 1;
        const turn: ChatTurn = {
          id: `${id}-t${index}`,
          conversation_id: id,
          client_message_id: body.client_message_id,
          user_message_id: `${id}-u${index}`,
          assistant_message_id: `${id}-a${index}`,
          status: "running",
          created_at: date,
          started_at: date,
          finished_at: null,
          error: null,
        };
        saved.turns.unshift(turn);
        saved.messages.push({
          id: turn.user_message_id,
          conversation_id: id,
          role: "user",
          content: body.message,
          status: "completed",
          created_at: date,
        });
        return route.fulfill({ status: 202, json: turn });
      }
      if (resource.endsWith("/events")) {
        const turn = saved.turns.find(
          (t) => resource === `turns/${t.id}/events`,
        )!;
        turn.status = "completed";
        turn.finished_at = date;
        saved.messages.push({
          id: turn.assistant_message_id,
          conversation_id: id,
          role: "assistant",
          content: `Saved reply ${saved.turns.length} in ${id}`,
          status: "completed",
          created_at: date,
        });
        return route.fulfill({
          contentType: "text/event-stream",
          body: `id: 1\ndata: ${JSON.stringify({ event: "done", status: "completed", assistant_message_id: turn.assistant_message_id })}\n\n`,
        });
      }
      if (resource === "messages")
        return route.fulfill({ json: saved.messages });
      if (resource.startsWith("messages/"))
        return route.fulfill({
          json: saved.messages.find((m) => resource === `messages/${m.id}`),
        });
      if (resource === "turns") return route.fulfill({ json: saved.turns });
      if (resource === "pending-actions") return route.fulfill({ json: [] });
      return route.fulfill({
        status: 404,
        json: { detail: "Unexpected chat request" },
      });
    });
    await page.goto(modelId ? "/?model=m1" : "/");
    const open = () =>
      page
        .getByRole("button", { name: "Open agent chat", exact: true })
        .click();
    const transcript = page.getByRole("log", { name: "Saved chat messages" });
    async function send(text: string, reply: string) {
      await page.getByLabel("Message the agent", { exact: true }).fill(text);
      await page.getByRole("button", { name: "Send", exact: true }).click();
      await expect(transcript).toContainText(reply);
    }
    await open();
    await send("Remember the label blue orchard", "Saved reply 1 in c1");
    // Second send must not create a conversation or discard the first pair.
    await send("What label did I give you?", "Saved reply 2 in c1");
    await expect(transcript).toContainText("Remember the label blue orchard");
    await expect(transcript).toContainText("Saved reply 1 in c1");
    await expect(transcript.locator("article")).toHaveCount(4);
    expect(created).toBe(1);
    await page
      .getByRole("button", { name: "Minimize chat", exact: true })
      .click();
    await page.getByRole("button", { name: "Datasets", exact: true }).click();
    await open();
    await send("Continue the same discussion", "Saved reply 3 in c1");
    // Return to the original context on reload: model contexts are intentionally separate.
    await page.goto(modelId ? "/?model=m1" : "/");
    await open();
    await expect(transcript).toContainText("Saved reply 3 in c1");
    await send("One more question", "Saved reply 4 in c1");
    expect(created).toBe(1);
    expect(submissions.map((s) => s.conversation)).toEqual([
      "c1",
      "c1",
      "c1",
      "c1",
    ]);
    expect(submissions.map((s) => s.modelId)).toEqual([
      modelId,
      modelId,
      modelId,
      modelId,
    ]);
    expect(new Set(submissions.map((s) => s.clientId)).size).toBe(4);
    await expect(transcript.locator("article")).toHaveCount(8);
    // Only an explicit New conversation may allocate another conversation here.
    await page
      .getByRole("button", { name: "New conversation", exact: true })
      .click();
    await expect(transcript).not.toContainText("blue orchard");
    expect(created).toBe(1);
    await send("Start a separate discussion", "Saved reply 1 in c2");
    expect(created).toBe(2);
    expect(submissions.at(-1)?.conversation).toBe("c2");
  });
}
