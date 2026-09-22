import test from "node:test";
import assert from "node:assert/strict";
import { MockUpstream } from "./fixtures/mock-upstream.js";
import { loadConfigFromEnv } from "../src/config.js";
import { createProxy } from "../src/server.js";
import { SessionStore } from "../src/session-store.js";

test("Server Lifecycle, Session Expiration, and Limits", async (t) => {
  await t.test("SessionStore expires and cleans sessions after TTL", async () => {
    const store = new SessionStore({
      ttlMs: 50, // very short TTL
      maxSessions: 10,
    });

    const session = store.createSession({ system_prompt: "hello" });
    assert.equal(store.size, 1);
    assert.ok(store.getSession(session.id) !== undefined);

    // Wait for TTL to expire
    await new Promise((r) => setTimeout(r, 60));

    // getSession should detect expiration and return undefined
    const expired = store.getSession(session.id);
    assert.equal(expired, undefined);
    assert.equal(store.size, 0);

    store.close();
  });

  await t.test("SessionStore evicts oldest when capacity is exceeded", async () => {
    const store = new SessionStore({
      ttlMs: 10000,
      maxSessions: 2,
    });

    const s1 = store.createSession({ name: "s1" });
    await new Promise((r) => setTimeout(r, 10));
    const s2 = store.createSession({ name: "s2" });
    assert.equal(store.size, 2);

    // Creating 3rd should evict oldest (s1)
    const s3 = store.createSession({ name: "s3" });
    assert.equal(store.size, 2);
    assert.equal(store.getSession(s1.id), undefined);
    assert.ok(store.getSession(s2.id) !== undefined);
    assert.ok(store.getSession(s3.id) !== undefined);

    store.close();
  });

  await t.test("Server start, address, and close lifecycle", async () => {
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

    const addr = proxy.address();
    assert.ok(addr !== null);
    assert.equal(addr.address, "127.0.0.1");
    assert.ok(addr.port > 0);

    // Verify basic health / endpoint response
    const res = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data: {} }),
    });
    assert.equal(res.status, 200);

    // Close
    await proxy.close();
    await upstream.close();

    // After close, connecting should fail
    await assert.rejects(async () => {
      await fetch(`${proxyUrl}/chatbbc/init_session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data: {} }),
      });
    });
  });
});
