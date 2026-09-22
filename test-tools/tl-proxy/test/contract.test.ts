import test from "node:test";
import assert from "node:assert/strict";
import { createProxy } from "../src/server.js";
import { loadConfigFromEnv } from "../src/config.js";
import { MockUpstream } from "./fixtures/mock-upstream.js";

test("TL Contract & Envelope Validation", async (t) => {
  const upstream = new MockUpstream();
  const upstreamUrl = await upstream.start();

  const config = loadConfigFromEnv({
    TL_PROXY_HOST: "127.0.0.1",
    TL_PROXY_PORT: "0",
    UPSTREAM_BASE_URL: upstreamUrl,
    UPSTREAM_API_KEY: "mock-key",
    LOG_LEVEL: "error",
  });

  const proxy = createProxy(config);
  const proxyUrl = await proxy.start();

  t.after(async () => {
    await proxy.close();
    await upstream.close();
  });

  await t.test("POST /chatbbc/init_session with valid system_prompt returns session_id", async () => {
    const res = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        appId: "test-app",
        trCode: "test-tr",
        trVersion: "1.0",
        timestamp: Date.now(),
        requestId: "req-init-1",
        data: {
          prompt_variables: [
            { name: "system_prompt", value: "You are a helpful assistant." },
          ],
        },
      }),
    });

    assert.equal(res.status, 200);
    const json = (await res.json()) as any;
    assert.equal(json.code, 0);
    assert.equal(json.message, "success");
    assert.ok(typeof json.data?.session_id === "string");
    assert.ok(json.data.session_id.length > 0);
  });

  await t.test("POST /chatbbc/init_session rejects duplicate variable names with 400", async () => {
    const res = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requestId: "req-dup-1",
        data: {
          prompt_variables: [
            { name: "system_prompt", value: "prompt 1" },
            { name: "system_prompt", value: "prompt 2" },
          ],
        },
      }),
    });

    assert.equal(res.status, 400);
    const json = (await res.json()) as any;
    assert.ok(json.error.includes("Duplicate variable"));
  });

  await t.test("POST /chatbbc/init_session rejects empty system_prompt with 400", async () => {
    const res = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requestId: "req-empty-sys",
        data: {
          prompt_variables: [{ name: "system_prompt", value: "   " }],
        },
      }),
    });

    assert.equal(res.status, 400);
    const json = (await res.json()) as any;
    assert.ok(json.error.includes("cannot be empty"));
  });

  await t.test("Rejects native tool control fields at root or data level with 400", async () => {
    const resRoot = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tools: [{ type: "function" }],
        data: { prompt_variables: [] },
      }),
    });
    assert.equal(resRoot.status, 400);

    const resData = await fetch(`${proxyUrl}/chatbbc/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          session_id: "fake",
          txt: "hello",
          tool_choice: "auto",
        },
      }),
    });
    assert.equal(resData.status, 400);
  });

  await t.test("POST /chatbbc/chat with unknown session returns 404", async () => {
    const res = await fetch(`${proxyUrl}/chatbbc/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          session_id: "non-existent-session",
          txt: "hello",
        },
      }),
    });
    assert.equal(res.status, 404);
  });

  await t.test("POST /chatbbc/chat with invalid stream type returns 400", async () => {
    const res = await fetch(`${proxyUrl}/chatbbc/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          session_id: "sess_1",
          txt: "hello",
          stream: "false", // string instead of boolean
        },
      }),
    });
    assert.equal(res.status, 400);
  });

  await t.test("POST /chatbbc/chat rejects real file attachments with 400", async () => {
    const res = await fetch(`${proxyUrl}/chatbbc/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          session_id: "sess_1",
          txt: "hello",
          files: [{ file_id: "file-123", url: "http://example.com/doc.pdf" }],
        },
      }),
    });
    assert.equal(res.status, 400);
    const json = (await res.json()) as any;
    assert.ok(json.error.includes("attachments are not supported"));
  });

  await t.test("Non-POST requests return 405 with Allow header", async () => {
    const res = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "GET",
    });
    assert.equal(res.status, 405);
    assert.equal(res.headers.get("allow"), "POST, OPTIONS");
  });

  await t.test("Non-streaming chat success returns data.txt in envelope", async () => {
    // 1. init session
    const initRes = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          prompt_variables: [
            { name: "system_prompt", value: "Act as an echo assistant." },
          ],
        },
      }),
    });
    const initJson = (await initRes.json()) as any;
    const sessionId = initJson.data.session_id;

    // 2. non-streaming chat
    const chatRes = await fetch(`${proxyUrl}/chatbbc/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          session_id: sessionId,
          txt: "Hello, GenSlide!",
          stream: false,
        },
      }),
    });

    assert.equal(chatRes.status, 200);
    const chatJson = (await chatRes.json()) as any;
    assert.equal(chatJson.code, 0);
    assert.equal(chatJson.message, "success");
    assert.equal(chatJson.data.txt, "Mock completion response");

    // Verify upstream received messages: system + user
    const lastUpstreamReq = upstream.recordedRequests.slice(-1)[0];
    assert.equal(lastUpstreamReq.body.messages.length, 2);
    assert.equal(lastUpstreamReq.body.messages[0].role, "system");
    assert.equal(lastUpstreamReq.body.messages[0].content, "Act as an echo assistant.");
    assert.equal(lastUpstreamReq.body.messages[1].role, "user");
    assert.equal(lastUpstreamReq.body.messages[1].content, "Hello, GenSlide!");
    assert.equal(lastUpstreamReq.body.stream, false);
  });
});
