import test from "node:test";
import assert from "node:assert/strict";
import { MockUpstream } from "./fixtures/mock-upstream.js";
import { loadConfigFromEnv } from "../src/config.js";
import { ProxyLogger } from "../src/logger.js";
import { createProxy } from "../src/server.js";

test("Four-Direction Debug Logging and Credential Masking", async (t) => {
  const upstream = new MockUpstream();
  const upstreamUrl = await upstream.start();

  const loggedLines: string[] = [];
  const customSink = (line: string) => {
    loggedLines.push(line);
  };

  const secretApiKey = "sk-super-secret-api-key-12345";
  const secretTestToken = "test-secret-bearer-999";

  const config = loadConfigFromEnv({
    TL_PROXY_HOST: "127.0.0.1",
    TL_PROXY_PORT: "0",
    UPSTREAM_BASE_URL: upstreamUrl,
    UPSTREAM_API_KEY: secretApiKey,
    AUTH_MODE: "bearer",
    TEST_ACCESS_TOKEN: secretTestToken,
    LOG_LEVEL: "debug",
  });

  const logger = new ProxyLogger({
    level: "debug",
    sensitiveTokens: [secretApiKey, secretTestToken],
    sink: customSink,
  });

  const proxy = createProxy(config, { logger });
  const proxyUrl = await proxy.start();

  t.after(async () => {
    await proxy.close();
    await upstream.close();
  });

  await t.test("Captures all 4 directions and redacts sensitive credentials", async () => {
    // 1. init_session
    const initRes = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${secretTestToken}`,
      },
      body: JSON.stringify({
        data: {
          prompt_variables: [
            { name: "system_prompt", value: "This is public system prompt instructions." },
          ],
        },
      }),
    });
    assert.equal(initRes.status, 200);
    const { data: { session_id } } = (await initRes.json()) as any;

    // 2. chat non-streaming
    const chatRes = await fetch(`${proxyUrl}/chatbbc/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${secretTestToken}`,
      },
      body: JSON.stringify({
        data: {
          session_id,
          txt: "User message asking for presentation outline",
          stream: false,
        },
      }),
    });
    assert.equal(chatRes.status, 200);

    // Verify logged lines
    const parsedLogs = loggedLines.map((l) => JSON.parse(l));

    const directions = new Set(parsedLogs.map((p) => p.direction));
    assert.ok(directions.has("client_to_proxy"), "Must have client_to_proxy logs");
    assert.ok(directions.has("proxy_to_upstream"), "Must have proxy_to_upstream logs");
    assert.ok(directions.has("upstream_to_proxy"), "Must have upstream_to_proxy logs");
    assert.ok(directions.has("proxy_to_client"), "Must have proxy_to_client logs");

    // Check credential redaction across all logs
    const fullLogText = loggedLines.join("\n");
    assert.equal(
      fullLogText.includes(secretApiKey),
      false,
      "Upstream API key must not appear unmasked in any log output"
    );
    assert.equal(
      fullLogText.includes(secretTestToken),
      false,
      "Test access token must not appear unmasked in any log output"
    );

    // Verify non-sensitive text is preserved
    assert.ok(
      fullLogText.includes("public system prompt instructions"),
      "Non-sensitive prompt must be preserved"
    );
    assert.ok(
      fullLogText.includes("User message asking for presentation outline"),
      "Non-sensitive user payload must be preserved"
    );
  });
});
