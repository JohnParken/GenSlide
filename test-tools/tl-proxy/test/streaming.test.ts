import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { AddressInfo } from "node:net";
import { createProxy } from "../src/server.js";
import { loadConfigFromEnv } from "../src/config.js";

test("Streaming SSE, Latch Test, and Error Handling", async (t) => {
  // Setup a custom upstream server for fine-grained stream control
  let upstreamReleaseLatch: (() => void) | null = null;
  let simulateUnexpectedToolCall = false;

  const upstreamServer = http.createServer((_req, res) => {
    if (simulateUnexpectedToolCall) {
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
      });
      res.write(
        `data: ${JSON.stringify({
          choices: [
            {
              delta: {
                content: "First token ",
                tool_calls: [{ id: "call_1", type: "function" }],
              },
            },
          ],
        })}\n\n`
      );
      res.end();
      return;
    }

    // Latch stream: send chunk 1, wait for upstreamReleaseLatch, then chunk 2, then DONE
    res.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache",
    });

    res.write(
      `data: ${JSON.stringify({
        choices: [{ delta: { content: "Part 1: Initial text. " } }],
      })}\n\n`
    );

    const checkAndSendRest = () => {
      res.write(
        `data: ${JSON.stringify({
          choices: [{ delta: { content: "Part 2: Final text." }, finish_reason: "stop" }],
        })}\n\n`
      );
      res.write("data: [DONE]\n\n");
      res.end();
    };

    if (upstreamReleaseLatch) {
      // Wait for release
      upstreamReleaseLatch = () => {
        checkAndSendRest();
      };
    } else {
      checkAndSendRest();
    }
  });

  const upstreamUrl = await new Promise<string>((resolve) => {
    upstreamServer.listen(0, "127.0.0.1", () => {
      const addr = upstreamServer.address() as AddressInfo;
      resolve(`http://127.0.0.1:${addr.port}`);
    });
  });

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
    await new Promise<void>((resolve, reject) => {
      upstreamServer.close((err) => (err ? reject(err) : resolve()));
    });
  });

  await t.test("Latch test: first chunk arrives before upstream finishes", async () => {
    // 1. init session
    const initRes = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          prompt_variables: [{ name: "system_prompt", value: "system instruction" }],
        },
      }),
    });
    const { data: { session_id } } = (await initRes.json()) as any;

    let released = false;
    upstreamReleaseLatch = () => {
      released = true;
    };

    // 2. Start stream
    const chatRes = await fetch(`${proxyUrl}/chatbbc/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        data: {
          session_id,
          txt: "Tell me a story",
          stream: true,
        },
      }),
    });

    assert.equal(chatRes.status, 200);
    assert.ok(chatRes.headers.get("content-type")?.includes("text/event-stream"));

    const reader = chatRes.body!.getReader();
    const decoder = new TextDecoder();

    let firstChunkReceived = false;
    let fullOutput = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      const chunkText = decoder.decode(value, { stream: true });
      fullOutput += chunkText;

      if (!firstChunkReceived && fullOutput.includes("Part 1: Initial text.")) {
        firstChunkReceived = true;
        // Verify latch has NOT been released yet by upstream!
        // This proves the chunk arrived BEFORE the upstream finished!
        assert.equal(released, false, "First chunk must arrive while upstream is still paused at latch");
        // Now release upstream to send part 2
        if (upstreamReleaseLatch) {
          upstreamReleaseLatch();
        }
      }
    }

    assert.ok(firstChunkReceived, "Must have received first chunk");
    assert.ok(fullOutput.includes("Part 2: Final text."));
    assert.ok(fullOutput.includes("event: done"));
    assert.ok(fullOutput.includes('{"finished":true}'));
  });

  await t.test("Stream with unexpected upstream tool_calls emits error event and closes without done", async () => {
    simulateUnexpectedToolCall = true;

    // 1. init session
    const initRes = await fetch(`${proxyUrl}/chatbbc/init_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          prompt_variables: [{ name: "system_prompt", value: "test sys" }],
        },
      }),
    });
    const { data: { session_id } } = (await initRes.json()) as any;

    const chatRes = await fetch(`${proxyUrl}/chatbbc/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: {
          session_id,
          txt: "Test tool call reject",
          stream: true,
        },
      }),
    });

    const bodyText = await chatRes.text();
    assert.ok(bodyText.includes("event: error"), "Must include error event");
    assert.ok(bodyText.includes("native tool_calls"), "Error message must mention native tool_calls");
    assert.ok(!bodyText.includes("event: done"), "Must NOT include done event on error");

    simulateUnexpectedToolCall = false;
  });
});
