import { test, expect, type Page } from "@playwright/test";
const model = {
  id: "m1",
  name: "Credit",
  version: "1",
  language: "R",
  dataset_id: "uci_credit",
  purpose: "Demo",
  owner: null,
  lifecycle_status: "development",
  created_at: "2026-11-07T12:00:00Z",
};
const baseTurn = {
  id: "t1",
  conversation_id: "c1",
  client_message_id: "client1",
  user_message_id: "u1",
  assistant_message_id: "a1",
  status: "running",
  created_at: model.created_at,
  started_at: null,
  finished_at: null,
  error: null,
};
const baseAction = {
  id: "p1",
  turn_id: "t1",
  action_version: "a".repeat(64),
  status: "pending",
  kind: "run",
  operation: "document",
  model_id: "m1",
  model_version: "1",
  allowed_decisions: ["approved", "rejected"],
  run_id: null,
  continuation_id: null,
  parameters: null,
};
async function setup(page: Page, mode = "complete") {
  const state = {
    run: { id: "document-run1", model_id: "m1", operation: "document", model_version: "1", dataset_id: "uci_credit", status: "running", created_at: model.created_at, started_at: null, finished_at: null, stage: "Writing", error: null as string | null },
    proposal: {
      id: "proposal1", source_run_id: null, source_task_id: "task1",
      conversation_id: "c1", model_id: "m1", type: "start_operation",
      args: { operation: "document" }, rationale: "Generate documentation",
      evidence: [{ kind: "file", ref: "manifest.json" }], required_role: "developer",
      preconditions: { model_version: "1" }, status: "pending",
      decision: null as Record<string, unknown> | null,
      result: null as Record<string, unknown> | null, created_at: model.created_at,
    },
    turnId: "t1",
    assistantId: "a1",
    replyText: "Canonical complete answer including missing tokens.",
    status: "running",
    readOffline: false,
    clientId: "client1",
    submitted: false,
    decided: false,
    stale: false,
    decisionError: false,
    role: "developer",
    posts: [] as { path: string; body: Record<string, unknown> }[],
    cursors: [] as string[],
    streams: 0,
  };
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(),
      u = new URL(req.url()),
      path = u.pathname,
      post = req.method() === "POST";
    if (post)
      state.posts.push({
        path,
        body: req.postData() ? req.postDataJSON() : {},
      });
    const turn = {
      ...baseTurn,
      id: state.turnId,
      assistant_message_id: state.assistantId,
      client_message_id: state.clientId,
      status: state.status,
      error: state.status === "failed" ? "llm_stream_error" : null,
    };
    const action = {
      ...baseAction,
      action_version: state.stale ? "b".repeat(64) : baseAction.action_version,
      ...(state.decided
        ? {
            status: "decided",
            allowed_decisions: [],
            run_id: "run1",
            continuation_id: "t2",
          }
        : {}),
    };
    const messages = [
      {
        id: "u1",
        conversation_id: "c1",
        role: "user",
        content: "Help",
        status: "completed",
        created_at: model.created_at,
      },
      {
        id: state.assistantId,
        conversation_id: "c1",
        role: "assistant",
        content:
          mode.startsWith("partial") && state.streams < 3 ? "" : state.status === "completed"
            ? state.replyText
            : "Saved pause text",
        status: state.status,
        created_at: model.created_at,
      },
    ];
    if (path === "/api/v1/runs/document-run1" || path === "/api/v1/runs/run1") return route.fulfill({ json: { ...state.run, id: path.split("/").at(-1) } });
    if (path === "/api/v1/proposals/actions") return route.fulfill({ json: {
      start_operation: { required_role: "developer", execution_available: true, args_schema: {} },
    }});
    if (path === "/api/v1/proposals") return route.fulfill({ json: [state.proposal] });
    if (path === "/api/v1/proposals/" + state.proposal.id) return route.fulfill({ json: state.proposal });
    if (path === "/api/v1/proposals/" + state.proposal.id + "/decision") {
      state.proposal.decision = req.postDataJSON();
      state.proposal.status = req.postDataJSON().decision;
      return route.fulfill({ json: state.proposal });
    }
    if (path === "/api/v1/proposals/" + state.proposal.id + "/execute") {
      state.proposal.status = "executed";
      state.proposal.result = { run_id: "document-run1" };
      return route.fulfill({ json: state.proposal });
    }
    if (path.endsWith("/auth/me"))
      return route.fulfill({
        json: {
          id: "chat-user",
          username: "user",
          role: state.role,
          active: true,
        },
      });
    if (path === "/api/v1/models") return route.fulfill({ json: [model] });
    if (path === "/api/v1/models/m1") return route.fulfill({ json: model });
    if (path === "/api/v1/conversations")
      return route.fulfill({
        status: post ? 201 : 200,
        json: post
          ? { id: "c1" }
          : [
              {
                id: "c1",
                actor_id: "chat-user",
                context: { model_id: "m1" },
                busy: false,
                updated_at: model.created_at,
              },
            ],
      });
    if (path.endsWith("/messages") && post) {
      state.submitted = true;
      state.clientId = req.postDataJSON().client_message_id;
      if (mode === "uncertain") {
        state.status = "completed";
        return route.abort("failed");
      }
      return route.fulfill({
        status: 202,
        json: { ...turn, client_message_id: state.clientId },
      });
    }
    if (!post && state.readOffline && path.includes("/conversations/")) return route.abort("internetdisconnected");
    if (path.endsWith("/messages"))
      return route.fulfill({ json: state.submitted ? messages : [] });
    if (path.endsWith("/messages/" + state.assistantId))
      return route.fulfill({ json: messages[1] });
    if (path.endsWith("/turns"))
      return route.fulfill({ json: state.submitted ? [turn] : [] });
    if (path.endsWith("/turns/t1/cancel")) return route.fulfill({json:turn});
    if (path.endsWith("/turns/t1")) return route.fulfill({ json: turn });
    if (path.endsWith("/pending-actions"))
      return route.fulfill({
        json: mode === "pause" && state.submitted ? [action] : [],
      });
    if (path.endsWith("/pending-actions/p1"))
      return route.fulfill({ json: action });
    if (path.endsWith("/pending-actions/p1/decision")) {
      if (state.decisionError)
        return route.fulfill({
          status: 409,
          json: { detail: "Action changed" },
        });
      state.decided = true;
      state.status = "resolved";
      return route.fulfill({
        json: {
          ...action,
          status: "decided",
          allowed_decisions: [],
          run_id: "run1",
          continuation_id: "t2",
        },
      });
    }
    if (path.endsWith("/events")) {
      state.streams++;
      state.cursors.push(u.searchParams.get("after_seq")!);
      if (mode.startsWith("partial") && state.streams < 3) {
        if (mode === "partial-failed" && state.streams === 2) state.status = "failed";
        return route.fulfill({ contentType: "text/event-stream",
          body: 'id: 4\ndata: {"event":"token","text":"Partial prefix"}\n\n' +
            (state.streams === 2 ? 'id: 5\ndata: {"event":"token","text":" and suffix"}\n\n' : ""),
        });
      }
      if (mode === "proposal") {
        state.status = "completed";
        const events = [
          { event: "review_required", text: "Review proposed documentation", proposal_id: state.proposal.id },
          { event: "done", status: "completed", assistant_message_id: state.assistantId },
        ];
        return route.fulfill({ contentType: "text/event-stream",
          body: events.map((event, i) => `id: ${i+1}\ndata: ${JSON.stringify(event)}\n\n`).join("") });
      }
      if (mode === "pause") {
        state.status = "awaiting_confirmation";
        return route.fulfill({
          contentType: "text/event-stream",
          body: 'id: 1\ndata: {"event":"confirmation_required","text":"Start documentation?","pending_id":"p1"}\n\n',
        });
      }
      if (mode === "reconnect" && state.streams === 1)
        return route.fulfill({
          contentType: "text/event-stream",
          body: 'id: 4\ndata: {"event":"token","text":"Partial only"}\n\n',
        });
      state.status = "completed";
      return route.fulfill({
        contentType: "text/event-stream",
        body: 'id: 4\ndata: {"event":"token","text":"Partial only"}\n\nid: 9\ndata: {"event":"done","status":"completed","assistant_message_id":"a1","text":"Do not trust this event text"}\n\n',
      });
    }
    return route.fulfill({ json: [] });
  });
  return state;
}
async function send(page: Page) {
  await page.goto("/?model=m1"); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  await page.getByLabel("Message the agent").fill("Help");
  await page.getByRole("button", { name: "Send", exact: true }).click();
}
test("durable chat replaces missing tokens with canonical reply and restores after reload without writes", async ({
  page,
}) => {
  const s = await setup(page);
  await send(page);
  await expect(
    page.getByRole("log", { name: "Saved chat messages" }),
  ).toContainText("Canonical complete answer");
  await expect(page.getByText("Do not trust this event text")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Provisional reply" }),
  ).toHaveCount(0);
  expect(s.posts.filter((p) => p.path.endsWith("/messages"))).toHaveLength(1);
  expect(s.posts[1].body.client_message_id).toBeTruthy();
  await page.reload(); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  await expect(page.getByRole("log")).toContainText(
    "Canonical complete answer",
  );
  expect(s.posts).toHaveLength(2);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
test("unsaved preview survives failed reads and cursor replay until canonical text arrives", async ({ page }) => {
  const s = await setup(page, "partial-running");
  await send(page);
  const preview = page.getByRole("region", { name: "Provisional reply" });
  await expect(preview).toContainText("Partial prefix");
  await expect(page.getByText(/Stream disconnected; reply may be incomplete/)).toBeVisible();
  s.readOffline = true;
  await page.getByRole("button", { name: "Refresh saved chat" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(preview).toContainText("Partial prefix");
  s.readOffline = false;
  await page.getByRole("button", { name: "Resume stream" }).click();
  await expect(preview).toContainText("Partial prefix and suffix");
  expect((await preview.innerText()).match(/Partial prefix/g)).toHaveLength(1);
  expect(s.cursors).toEqual(["0", "4"]);
  await page.getByRole("button", { name: "Resume stream" }).click();
  await expect(page.getByRole("log")).toContainText("Canonical complete answer");
  await expect(preview).toHaveCount(0);
  expect(s.cursors).toEqual(["0", "4", "5"]);
  expect(s.posts.filter(p => p.path.endsWith("/messages"))).toHaveLength(1);
});

test("failed saved turn without text keeps an unsaved preview and never claims reply restoration", async ({ page }) => {
  const s = await setup(page, "partial-failed");
  await send(page);
  await expect(page.getByText(/Stream disconnected; reply may be incomplete/)).toBeVisible();
  await page.getByRole("button", { name: "Resume stream" }).click();
  await expect(page.getByText(/Saved turn status: failed/)).toContainText("server has no saved reply text");
  await expect(page.getByText(/Response status: failed/)).toContainText("llm_stream_error");
  const preview = page.getByRole("region", { name: "Provisional reply" });
  await expect(preview).toContainText("Partial prefix and suffix");
  await expect(preview).toContainText("Unsaved streaming preview");
  await expect(page.getByRole("log")).not.toContainText("Partial prefix");
  await expect(page.getByText(/restored the saved turn and canonical reply/)).toHaveCount(0);
  await page.getByRole("button", { name: "Refresh saved chat" }).click();
  await expect(preview).toContainText("Partial prefix and suffix");
  expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage }))).not.toContain("Partial prefix");
  await page.getByRole("button", { name: "New conversation", exact: true }).click();
  await expect(preview).toHaveCount(0);
  expect(s.posts.filter(p => p.path.endsWith("/messages"))).toHaveLength(1);
});

test("interrupted observation resumes at last event cursor without resubmission", async ({
  page,
}) => {
  const s = await setup(page, "reconnect");
  await send(page);
  await expect(
    page.getByText(/Stream disconnected; reply may be incomplete/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Resume stream" }).click();
  await expect(page.getByRole("log")).toContainText(
    "Canonical complete answer",
  );
  expect(s.cursors).toEqual(["0", "4"]);
  expect(s.posts.filter((p) => p.path.endsWith("/messages"))).toHaveLength(1);
});
test("uncertain submission restores the acknowledged turn after reload using reads only", async ({
  page,
}) => {
  const s = await setup(page, "uncertain");
  await send(page);
  await expect(page.getByText(/Submission outcome is uncertain/)).toBeVisible();
  await page.reload(); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  await expect(page.getByRole("log")).toContainText(
    "Canonical complete answer",
  );
  await expect(page.getByText(/Submission outcome is uncertain/)).toHaveCount(
    0,
  );
  expect(s.posts.filter((p) => p.path.endsWith("/messages"))).toHaveLength(1);
});
test("pending confirmation survives refresh, blocks chat approval and posts one exact decision", async ({
  page,
}) => {
  const s = await setup(page, "pause");
  await send(page);
  await expect(
    page.getByRole("heading", { name: "Confirm proposed document" }),
  ).toBeVisible();
  await page.reload(); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  await expect(
    page.getByRole("heading", { name: "Confirm proposed document" }),
  ).toBeVisible();
  await page.getByLabel("Message the agent").fill("yes");
  await expect(
    page.getByRole("button", { name: "Send", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("Action decision", { exact: true })
    .selectOption("approved");
  await page
    .getByLabel("Action rationale", { exact: true })
    .fill("Generate the model document");
  await page
    .getByLabel(
      "I confirm the exact action, model version, parameters and decision shown above",
    )
    .check();
  await page.getByRole("button", { name: "Submit action decision" }).click();
  await expect(page.getByText(/Decision recorded/)).toBeVisible();
  const writes = s.posts.filter((p) => p.path.endsWith("/decision"));
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toEqual({
    client_decision_id: expect.any(String),
    action_version: baseAction.action_version,
    outcome: "approved",
    rationale: "Generate the model document",
  });
});
test("changed action blocks POST and an uncertain decision needs a fresh inspection", async ({
  page,
}) => {
  const s = await setup(page, "pause");
  await send(page);
  await expect(
    page.getByRole("heading", { name: "Confirm proposed document" }),
  ).toBeVisible();
  await page
    .getByLabel("Action decision", { exact: true })
    .selectOption("approved");
  await page.getByLabel("Action rationale", { exact: true }).fill("Proceed");
  await page
    .getByLabel(
      "I confirm the exact action, model version, parameters and decision shown above",
    )
    .check();
  s.stale = true;
  await page.getByRole("button", { name: "Submit action decision" }).click();
  await expect(page.getByRole("alert")).toContainText("Action changed");
  expect(s.posts.filter((p) => p.path.endsWith("/decision"))).toHaveLength(0);
  await expect(
    page.getByRole("button", { name: "Submit action decision" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Refresh saved chat" }).click();
  await page
    .getByLabel("Action decision", { exact: true })
    .selectOption("approved");
  await page
    .getByLabel("Action rationale", { exact: true })
    .fill("Proceed updated");
  await page
    .getByLabel(
      "I confirm the exact action, model version, parameters and decision shown above",
    )
    .check();
  s.decisionError = true;
  await page.getByRole("button", { name: "Submit action decision" }).click();
  await expect(page.getByRole("alert")).toContainText("Action changed");
  expect(s.posts.filter((p) => p.path.endsWith("/decision"))).toHaveLength(1);
});
test("approver cannot authorize a developer operation", async ({
  page,
}) => {
  const s = await setup(page, "pause");
  s.role = "approver";
  await send(page);
  await expect(
    page.getByRole("heading", { name: "Confirm proposed document" }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Action decision").locator('option[value="approved"]'),
  ).toHaveAttribute("disabled", "");
  expect(s.posts.filter((p) => p.path.endsWith("/decision"))).toHaveLength(0);
});

test("changing signed-in account does not restore another account's selected chat",async({page})=>{
  const s=await setup(page);await send(page);
  await expect(page.getByRole("log")).toContainText("Canonical complete answer");
  await page.route("**/api/v1/auth/me",route=>route.fulfill({json:{id:"another-account",username:"another",role:"developer",active:true}}));
  await page.reload(); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  await expect(page.getByRole("log")).toContainText("How can I help?");
  await expect(page.getByRole("log")).not.toContainText("Canonical complete answer");
  expect(s.posts).toHaveLength(2);
});
test("server cancellation requires explicit confirmation and terminal state is read separately",async({page})=>{
  const s=await setup(page,"reconnect");await send(page);
  await expect(page.getByText(/Stream disconnected; reply may be incomplete/)).toBeVisible();
  await expect(page.getByRole("button",{name:"Cancel server turn"})).toBeDisabled();
  await page.getByLabel("Request server cancellation of this turn").check();
  await page.getByRole("button",{name:"Cancel server turn"}).click();
  await expect(page.getByText(/Cancellation requested/)).toBeVisible();
  await expect(page.getByText("Response status: running")).toBeVisible();
  expect(s.posts.filter(p=>p.path.endsWith("/cancel"))).toHaveLength(1);
  s.status="cancelled";
  await page.getByRole("button",{name:"Refresh saved chat"}).click();
  await expect(page.getByText("Response status: cancelled")).toBeVisible();
  await expect(page.getByRole("button",{name:"Cancel server turn"})).toHaveCount(0);
  expect(s.posts.filter(p=>p.path.endsWith("/messages"))).toHaveLength(1);
});


test("agent replies render Markdown safely inside a narrow chat window", async ({ page }) => {
  const s = await setup(page);
  s.replyText = [
    "## 上传数据集",
    "上传包含 **三个文件**，使用 `dataset.json`。",
    "1. 原始数据\n2. 描述文件\n3. 分割文件",
    "```python\nprint('" + "long-value-".repeat(40) + "')\n```",
    "| File | Purpose |\n| --- | --- |\n| dataset.json | Descriptor |",
    "[Documentation](https://example.com/docs)",
    "[Unsafe](javascript:alert(1))",
    "![Remote image](https://example.com/never-load.png)",
    '<img src="https://example.com/raw-never-load.png" onerror="alert(1)">',
    "- [ ] Suggested task",
  ].join("\n\n");
  const externalRequests: string[] = [];
  await page.route("https://example.com/**", route => {
    externalRequests.push(route.request().url());
    return route.abort();
  });
  await send(page);
  const reply = page.getByRole("log").locator(".chat-message-assistant");
  await expect(reply.getByRole("heading", { name: "上传数据集" })).toBeVisible();
  await expect(reply.locator("strong")).toHaveText("三个文件");
  await expect(reply.locator("ol li")).toHaveCount(3);
  await expect(reply.locator("pre code")).toContainText("print(");
  await expect(reply.getByRole("table")).toContainText("Descriptor");
  await expect(reply.getByRole("link", { name: "Documentation" })).toHaveAttribute("rel", "noopener noreferrer");
  await expect(reply.getByRole("link", { name: "Unsafe" })).toHaveCount(0);
  await expect(reply.locator("img, script")).toHaveCount(0);
  await expect(reply.getByRole("checkbox")).toBeDisabled();
  for (const size of [{ width: 960, height: 540 }, { width: 390, height: 700 }]) {
    await page.setViewportSize(size);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const box = await reply.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(size.width);
    await reply.getByRole("heading", { name: "上传数据集" }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/chat-markdown-${size.width}.png` });
  }
  expect(externalRequests).toEqual([]);
  expect(s.posts.filter(p => p.path.endsWith("/messages"))).toHaveLength(1);
});


test("chat-delivered proposal requires separate explicit approval and execution", async ({ page }) => {
  const s = await setup(page, "proposal");
  await send(page);
  const card = page.getByRole("region", { name: "Saved proposal", exact: true });
  await expect(card.getByRole("heading", { name: "Start a model task" })).toBeVisible();
  expect(s.posts.filter(p => p.path.includes("/proposals/"))).toHaveLength(0);
  await card.getByRole("combobox", { name: "Proposal decision", exact: true }).selectOption("approved");
  await card.getByLabel("Decision rationale").fill("Inspect the uploaded model");
  await card.getByLabel("I inspected the evidence and confirm this exact proposal and decision").check();
  await card.getByRole("button", { name: "Submit proposal decision" }).click();
  await expect(card.getByRole("button", { name: "Execute approved action", exact: true })).toBeDisabled();
  expect(s.posts.filter(p => p.path.endsWith("/execute"))).toHaveLength(0);
  await card.getByLabel("I confirm execution of this exact approved action").check();
  await card.getByRole("button", { name: "Execute approved action", exact: true }).click();
  await expect(card.getByRole("region", { name: "Proposal outcome" })).toContainText("document-run1");
  expect(s.posts.filter(p => p.path.endsWith("/proposal1/decision"))).toHaveLength(1);
  expect(s.posts.filter(p => p.path.endsWith("/proposal1/execute"))).toHaveLength(1);
});

for (const mismatch of ["model_id", "conversation_id"] as const) {
  test(`chat proposal from another ${mismatch} cannot become actionable`, async ({ page }) => {
    const s = await setup(page, "proposal");
    s.proposal[mismatch] = "another-context";
    await send(page);
    const card = page.getByRole("region", { name: "Saved proposal", exact: true });
    await expect(card.getByRole("alert")).toContainText("another context");
    await expect(card.getByRole("button", { name: "Submit proposal decision" })).toHaveCount(0);
    expect(s.posts.filter(p => p.path.includes("/proposals/"))).toHaveLength(0);
  });
}

test("saved chat proposals recover after reload without replaying a write or approval draft", async ({ page }) => {
  const s = await setup(page, "proposal");
  await send(page);
  const card = page.getByRole("region", { name: "Saved proposal", exact: true });
  await card.getByRole("combobox", { name: "Proposal decision", exact: true }).selectOption("approved");
  await card.getByLabel("Decision rationale").fill("Unsubmitted local draft");
  await card.getByLabel("I inspected the evidence and confirm this exact proposal and decision").check();
  await page.reload();
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await expect(page.getByRole("log")).toContainText("Canonical complete answer");
  await page.getByRole("button", { name: "Browse saved agent proposals" }).click();
  await page.getByRole("button", { name: /Start a model task · pending/ }).click();
  await expect(card.getByLabel("Decision rationale")).toHaveValue("");
  await expect(card.getByLabel("I inspected the evidence and confirm this exact proposal and decision")).not.toBeChecked();
  await expect(card.getByRole("button", { name: "Submit proposal decision" })).toBeDisabled();
  expect(s.posts.filter(p => p.path.includes("/proposals/"))).toHaveLength(0);
  expect(s.posts.filter(p => p.path.endsWith("/messages"))).toHaveLength(1);
});

for (const long of [false, true]) test(`proposal rejection supports real clipboard paste and confirmation (${long ? "long" : "short"})`, async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const s = await setup(page, "proposal");
  await page.setViewportSize({ width: 900, height: 800 });
  await send(page);
  const card = page.getByRole("region", { name: "Saved proposal", exact: true });
  await card.getByRole("combobox", { name: "Proposal decision", exact: true }).selectOption("rejected");
  const text = long ? "Please revise the model assumptions. ".repeat(100) : "E2E C3.3: approve document regeneration.\n\n";
  await page.evaluate(value => navigator.clipboard.writeText(value), text);
  const reason = card.getByLabel("Decision rationale");
  await reason.click();
  await reason.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
  await expect(reason).toHaveValue(text.slice(0, 2000));
  const confirmation = card.getByLabel("I inspected the evidence and confirm this exact proposal and decision");
  await confirmation.check();
  await expect(confirmation).toBeChecked();
  await card.getByRole("button", { name: "Submit proposal decision" }).click();
  await expect(card).toContainText("This proposal is rejected");
  expect(s.posts.filter(p => p.path.endsWith("/proposal1/decision"))).toHaveLength(1);
  expect(s.posts.filter(p => p.path.endsWith("/execute"))).toHaveLength(0);
  expect(errors).toEqual([]);
});

test("rejection followed by regeneration and saved-chat/proposal refresh stays interactive", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const errors: string[] = [];
  page.on("pageerror", e => errors.push(e.message));
  const s = await setup(page, "proposal");
  await send(page);
  const card = page.getByRole("region", { name: "Saved proposal", exact: true });
  await card.getByRole("combobox", { name: "Proposal decision", exact: true }).selectOption("rejected");
  await card.getByLabel("Decision rationale").fill("Please regenerate");
  await card.getByLabel("I inspected the evidence and confirm this exact proposal and decision").check();
  await card.getByRole("button", { name: "Submit proposal decision" }).click();
  await expect(card).toContainText("This proposal is rejected");
  s.turnId = "t2";
  s.assistantId = "a2";
  s.status = "running";
  s.proposal = { ...s.proposal, id: "proposal2", source_task_id: "task2", status: "pending", decision: null };
  await page.getByLabel("Message the agent").fill("Regenerate");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(card.getByRole("combobox", { name: "Proposal decision", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Refresh saved chat", exact: true }).click();
  await card.getByRole("button", { name: "Reload saved proposal", exact: true }).click();
  await card.getByRole("combobox", { name: "Proposal decision", exact: true }).selectOption("approved");
  const sample = "E2E C3.3: approve document regeneration.\n\n";
  await page.evaluate(value => navigator.clipboard.writeText(value), sample);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await card.getByLabel("Decision rationale").click();
  await card.getByLabel("Decision rationale").press(process.platform === "darwin" ? "Meta+V" : "Control+V");
  await expect(card.getByLabel("Decision rationale")).toHaveValue(sample);
  await card.getByLabel("I inspected the evidence and confirm this exact proposal and decision").check();
  await card.getByRole("button", { name: "Submit proposal decision" }).click();
  await expect(card.getByRole("button", { name: "Execute approved action", exact: true })).toBeDisabled();
  await page.reload();
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await page.getByRole("button", { name: "Browse saved agent proposals", exact: true }).click();
  await page.getByRole("button", { name: "Refresh proposals", exact: true }).click();
  await page.getByRole("button", { name: /Start a model task/ }).click();
  await expect(card.getByRole("button", { name: "Execute approved action", exact: true })).toBeDisabled();
  expect(s.posts.filter(p => p.path.endsWith("/decision"))).toHaveLength(2);
  expect(s.posts.filter(p => p.path.endsWith("/execute"))).toHaveLength(0);
  expect(errors).toEqual([]);
});

test("saved proposal refresh keeps one panel and supports pasted rationale with no React key errors", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  const s = await setup(page, "proposal");
  await page.goto("/?model=m1");
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await page.getByRole("button", { name: "Browse saved agent proposals", exact: true }).click();
  const list = page.getByRole("region", { name: "Agent proposals", exact: true });
  await expect(list.getByRole("button", { name: /Start a model task/ })).toBeVisible();
  expect(errors).toEqual([]);
  for (let i = 0; i < 3; i++) {
    await list.getByRole("button", { name: "Refresh proposals", exact: true }).click();
    await expect(list.getByRole("button", { name: /Start a model task/ })).toBeVisible();
    await page.getByRole("button", { name: "Hide agent proposals", exact: true }).click();
    await expect(list).toHaveCount(0);
    await page.getByRole("button", { name: "Browse saved agent proposals", exact: true }).click();
    await expect(list).toHaveCount(1);
  }
  await list.getByRole("button", { name: /Start a model task/ }).click();
  const card = list.getByRole("region", { name: "Saved proposal", exact: true });
  await card.getByRole("combobox", { name: "Proposal decision", exact: true }).selectOption("rejected");
  const text = "E2E C3.3: approve document regeneration.\n\n";
  await page.evaluate(value => navigator.clipboard.writeText(value), text);
  const reason = card.getByLabel("Decision rationale");
  await reason.click();
  await reason.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
  await expect(reason).toHaveValue(text);
  const confirmation = card.getByLabel("I inspected the evidence and confirm this exact proposal and decision");
  await confirmation.check();
  await expect(confirmation).toBeChecked();
  await page.getByRole("button", { name: "Open review cards", exact: true }).click();
  await page.getByRole("button", { name: "Hide review cards", exact: true }).click();
  await expect(reason).toHaveValue(text);
  await expect(confirmation).toBeChecked();
  await card.getByRole("button", { name: "Submit proposal decision", exact: true }).click();
  await expect(card).toContainText("This proposal is rejected");
  await list.getByRole("button", { name: "Refresh proposals", exact: true }).click();
  await expect(list.getByRole("button", { name: /Start a model task · rejected/ })).toBeVisible();
  await expect(list).toHaveCount(1);
  expect(errors).toEqual([]);
  expect(s.posts.filter(p => p.path.endsWith("/decision"))).toHaveLength(1);
  expect(s.posts.filter(p => p.path.endsWith("/execute"))).toHaveLength(0);
});

test("expired session during proposal refresh exits the chat without writes or a frozen form", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const state = await setup(page, "proposal");
  await page.goto("/?model=m1");
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await page.getByRole("button", { name: "Browse saved agent proposals", exact: true }).click();
  const list = page.getByRole("region", { name: "Agent proposals", exact: true });
  await list.getByRole("button", { name: /Start a model task/ }).click();
  await list.getByLabel("Decision rationale").fill("E2E C3.3: approve document regeneration.");
  await page.route("**/api/v1/proposals?**", route => route.fulfill({ status: 401, json: { detail: "Session expired" } }));
  await list.getByRole("button", { name: "Refresh proposals", exact: true }).click();
  await expect(page.getByText("Your session expired. Sign in again.", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Username", { exact: true })).toBeEditable();
  await expect(list).toHaveCount(0);
  expect(errors).toEqual([]);
  expect(state.posts).toHaveLength(0);
});

test("proposal list updates automatically without clearing decision input or sending writes", async ({ page }) => {
  const state = await setup(page, "proposal");
  await page.goto("/?model=m1");
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await page.getByRole("button", { name: "Browse saved agent proposals", exact: true }).click();
  const list = page.getByRole("region", { name: "Agent proposals", exact: true });
  await list.getByRole("button", { name: /Start a model task/ }).click();
  await list.getByLabel("Decision rationale").fill("Keep my inspection notes");
  await page.route("**/api/v1/proposals?**", route => route.fulfill({ json: [state.proposal, { ...state.proposal, id: "new-proposal" }] }));
  await expect(list.getByRole("button", { name: /Start a model task/ })).toHaveCount(2, { timeout: 15000 });
  await expect(list.getByLabel("Decision rationale")).toHaveValue("Keep my inspection notes");
  expect(state.posts).toHaveLength(0);
});

test("interrupted observation automatically resumes at last event cursor without resubmission", async ({
  page,
}) => {
  const s = await setup(page, "reconnect");
  await send(page);
  await expect(
    page.getByText(/Stream disconnected; reply may be incomplete/),
  ).toBeVisible();
  await expect(page.getByRole("log")).toContainText(
    "Canonical complete answer", { timeout: 12000 },
  );
  expect(s.cursors).toEqual(["0", "4"]);
  expect(s.posts.filter((p) => p.path.endsWith("/messages"))).toHaveLength(1);
});

test("changed proposal details preserve notes but block decisions until reinspection", async ({ page }) => {
  const state = await setup(page, "proposal");
  await page.goto("/?model=m1");
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await page.getByRole("button", { name: "Browse saved agent proposals", exact: true }).click();
  const list = page.getByRole("region", { name: "Agent proposals", exact: true });
  await list.getByRole("button", { name: /Start a model task/ }).click();
  await list.getByRole("combobox", { name: "Proposal decision", exact: true }).selectOption("rejected");
  await list.getByLabel("Decision rationale").fill("My review notes");
  await list.getByLabel("I inspected the evidence and confirm this exact proposal and decision").check();
  state.proposal = { ...state.proposal, status: "rejected" };
  await expect(list.getByText(/This proposal changed/)).toBeVisible({ timeout: 15000 });
  await expect(list.getByLabel("Decision rationale")).toHaveValue("My review notes");
  await expect(list.getByRole("button", { name: "Submit proposal decision", exact: true })).toBeDisabled();
  expect(state.posts).toHaveLength(0);
});

test("chat operation progresses independently and failed validation prepares a retry without writes", async ({ page }) => {
  const s = await setup(page, "proposal");
  s.role = "validator";
  s.proposal.status = "executed";
  s.proposal.result = { run_id: "document-run1" };
  s.run.operation = "validate";
  await send(page);
  const card = page.getByRole("region", { name: "Operation status document-run1", exact: true });
  await expect(card).toContainText("validate · running");
  s.run.status = "failed";
  s.run.error = "turn_limit_exceeded";
  await expect(card).toContainText("turn_limit_exceeded", { timeout: 10000 });
  await page.getByLabel("Message the agent").fill("My notes");
  const before = s.posts.length;
  await card.getByRole("button", { name: "Prepare retry request" }).click();
  await expect(page.getByLabel("Message the agent")).toHaveValue(/My notes[\s\S]*Inspect failed validate run document-run1/);
  expect(s.posts).toHaveLength(before);
  await card.getByRole("button", { name: "Refresh operation status" }).click();
  await expect(card).toContainText("turn_limit_exceeded");
  expect(s.posts).toHaveLength(before);
  await page.reload();
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await page.getByRole("button", { name: "Browse saved agent proposals", exact: true }).click();
  await page.getByRole("button", { name: /Start a model task.*executed/ }).click();
  await expect(card).toContainText("turn_limit_exceeded");
  expect(s.posts).toHaveLength(before);
});
test("chat operation card rejects mismatched run models and never offers retry", async ({ page }) => {
  const s = await setup(page, "proposal");
  s.proposal.status = "executed";
  s.proposal.result = { run_id: "document-run1" };
  s.run.model_id = "another-model";
  s.run.status = "failed";
  await send(page);
  const card = page.getByRole("region", { name: "Operation status document-run1", exact: true });
  await expect(card).toContainText("Operation belongs to another model or run");
  await expect(card.getByRole("button", { name: "Prepare retry request" })).toHaveCount(0);
  expect(s.posts.filter(p => p.path.includes("/runs"))).toHaveLength(0);
});

test("reviewer changes requested is not an execution failure or retry invitation", async ({ page }) => {
  const s = await setup(page, "proposal");
  s.proposal.status = "executed";
  s.proposal.result = { run_id: "document-run1" };
  s.run.status = "failed";
  s.run.stage = "changes_requested";
  s.run.error = null;
  await send(page);
  const card = page.getByRole("region", { name: "Operation status document-run1", exact: true });
  await expect(card).toContainText("Changes requested");
  await expect(card).toContainText("Check History for follow-up work");
  await expect(card).not.toContainText("The operation failed");
  await expect(card.getByRole("button", { name: "Prepare retry request" })).toHaveCount(0);
  const before = s.posts.length;
  await card.getByRole("button", { name: "Refresh operation status" }).click();
  await expect(card).toContainText("Changes requested");
  expect(s.posts).toHaveLength(before);
  await page.reload();
  await page.getByRole("button", { name: "Open agent chat", exact: true }).click();
  await page.getByRole("button", { name: "Browse saved agent proposals", exact: true }).click();
  await page.getByRole("button", { name: /Start a model task.*executed/ }).click();
  await expect(card).toContainText("Changes requested");
  await expect(card.getByRole("button", { name: "Prepare retry request" })).toHaveCount(0);
  expect(s.posts).toHaveLength(before);
});
