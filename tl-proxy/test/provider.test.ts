import test from "node:test";
import assert from "node:assert/strict";
import { MockUpstream } from "./fixtures/mock-upstream.js";
import { loadConfigFromEnv } from "../src/config.js";
import { ProxyLogger } from "../src/logger.js";
import { QwenProvider } from "../src/providers/qwen.js";
import { DeepSeekProvider } from "../src/providers/deepseek.js";

test("Provider Adapters & Thinking Mapping", async (t) => {
  const upstream = new MockUpstream();
  const upstreamUrl = await upstream.start();

  t.after(async () => {
    await upstream.close();
  });

  await t.test("QwenProvider maps enable_thinking correctly", async () => {
    // 1. Thinking enabled
    const configEnabled = loadConfigFromEnv({
      UPSTREAM_PROVIDER: "qwen",
      UPSTREAM_MODEL: "qwen3.8-flash",
      UPSTREAM_BASE_URL: upstreamUrl,
      UPSTREAM_API_KEY: "test-qwen-key",
      UPSTREAM_THINKING: "enabled",
      LOG_LEVEL: "error",
    });
    const logger = new ProxyLogger({ level: "error" });
    const qwenEnabled = new QwenProvider(configEnabled, logger);

    await qwenEnabled.complete(
      { messages: [{ role: "user", content: "hi" }] },
      { requestId: "req-qwen-1" }
    );

    let lastReq = upstream.recordedRequests.slice(-1)[0];
    assert.equal(lastReq.body.enable_thinking, true);
    assert.equal(lastReq.body.model, "qwen3.8-flash");

    // 2. Thinking default (not sent)
    const configDefault = loadConfigFromEnv({
      UPSTREAM_PROVIDER: "qwen",
      UPSTREAM_MODEL: "qwen3.8-flash",
      UPSTREAM_BASE_URL: upstreamUrl,
      UPSTREAM_API_KEY: "test-qwen-key",
      UPSTREAM_THINKING: "provider-default",
      LOG_LEVEL: "error",
    });
    const qwenDefault = new QwenProvider(configDefault, logger);

    await qwenDefault.complete(
      { messages: [{ role: "user", content: "hi" }] },
      { requestId: "req-qwen-2" }
    );

    lastReq = upstream.recordedRequests.slice(-1)[0];
    assert.equal(lastReq.body.enable_thinking, undefined);
  });

  await t.test("DeepSeekProvider maps thinking object correctly", async () => {
    // 1. Thinking enabled
    const configEnabled = loadConfigFromEnv({
      UPSTREAM_PROVIDER: "deepseek",
      UPSTREAM_MODEL: "deepseek-v4-flash",
      UPSTREAM_BASE_URL: upstreamUrl,
      UPSTREAM_API_KEY: "test-ds-key",
      UPSTREAM_THINKING: "enabled",
      LOG_LEVEL: "error",
    });
    const logger = new ProxyLogger({ level: "error" });
    const dsEnabled = new DeepSeekProvider(configEnabled, logger);

    await dsEnabled.complete(
      { messages: [{ role: "user", content: "hi" }] },
      { requestId: "req-ds-1" }
    );

    let lastReq = upstream.recordedRequests.slice(-1)[0];
    assert.deepEqual(lastReq.body.thinking, { type: "enabled" });
    assert.equal(lastReq.body.model, "deepseek-v4-flash");

    // 2. Thinking disabled
    const configDisabled = loadConfigFromEnv({
      UPSTREAM_PROVIDER: "deepseek",
      UPSTREAM_MODEL: "deepseek-v4-flash",
      UPSTREAM_BASE_URL: upstreamUrl,
      UPSTREAM_API_KEY: "test-ds-key",
      UPSTREAM_THINKING: "disabled",
      LOG_LEVEL: "error",
    });
    const dsDisabled = new DeepSeekProvider(configDisabled, logger);

    await dsDisabled.complete(
      { messages: [{ role: "user", content: "hi" }] },
      { requestId: "req-ds-2" }
    );

    lastReq = upstream.recordedRequests.slice(-1)[0];
    assert.deepEqual(lastReq.body.thinking, { type: "disabled" });
  });

  await t.test("Rejects unexpected upstream native tool_calls in non-streaming response", async () => {
    upstream.customHandler = (_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          choices: [
            {
              message: {
                role: "assistant",
                content: "",
                tool_calls: [{ id: "call_1", type: "function" }],
              },
            },
          ],
        })
      );
    };

    const config = loadConfigFromEnv({
      UPSTREAM_BASE_URL: upstreamUrl,
      UPSTREAM_API_KEY: "key",
      LOG_LEVEL: "error",
    });
    const logger = new ProxyLogger({ level: "error" });
    const provider = new QwenProvider(config, logger);

    await assert.rejects(
      async () => {
        await provider.complete(
          { messages: [{ role: "user", content: "hi" }] },
          { requestId: "req-fail-tool" }
        );
      },
      (err: any) => {
        assert.equal(err.statusCode, 502);
        assert.ok(err.safeMessage.includes("native tool_calls"));
        return true;
      }
    );

    upstream.customHandler = undefined;
  });
});
