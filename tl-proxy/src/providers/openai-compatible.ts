/**
 * OpenAI-compatible completions provider adapter
 */

import { ProxyConfig } from "../config.js";
import { ProxyLogger } from "../logger.js";
import { UpstreamError, UpstreamTimeoutError } from "../errors.js";
import { parseSSEStream } from "./sse.js";
import {
  ProviderAdapter,
  ProviderChatMessage,
  ProviderCompleteOptions,
  ProviderResponse,
  ProviderStreamEvent,
  ProviderStreamOptions,
  RequestMeta,
} from "./types.js";

export abstract class BaseOpenAICompatibleProvider implements ProviderAdapter {
  abstract readonly providerName: string;
  readonly modelName: string;
  protected config: ProxyConfig;
  protected logger: ProxyLogger;

  constructor(config: ProxyConfig, logger: ProxyLogger) {
    this.config = config;
    this.logger = logger;
    this.modelName = config.upstreamModel;
  }

  protected getEndpointUrl(): string {
    let base = this.config.upstreamBaseUrl.replace(/\/+$/, "");
    if (!base.endsWith("/chat/completions")) {
      base = `${base}/chat/completions`;
    }
    return base;
  }

  /**
   * Hook for sub-classes to inject provider-specific payload properties
   * (e.g., thinking parameters)
   */
  protected abstract getExtraPayloadFields(): Record<string, unknown>;

  protected buildRequestBody(messages: ProviderChatMessage[], stream: boolean): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      model: this.modelName,
      messages: messages.map((m) => ({
        role: m.role,
        content: m.content,
      })),
      stream,
      ...this.getExtraPayloadFields(),
    };

    if (this.config.upstreamMaxTokens !== undefined) {
      payload.max_tokens = this.config.upstreamMaxTokens;
    }

    return payload;
  }

  protected getRequestHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    if (this.config.upstreamApiKey) {
      headers["Authorization"] = `Bearer ${this.config.upstreamApiKey}`;
    }
    return headers;
  }

  public async complete(options: ProviderCompleteOptions, meta: RequestMeta): Promise<ProviderResponse> {
    const url = this.getEndpointUrl();
    const headers = this.getRequestHeaders();
    const bodyObj = this.buildRequestBody(options.messages, false);
    const bodyStr = JSON.stringify(bodyObj);

    this.logger.debug({
      requestId: meta.requestId,
      sessionId: meta.sessionId,
      provider: this.providerName,
      model: this.modelName,
      direction: "proxy_to_upstream",
      stage: "complete_request",
      url,
      method: "POST",
      headers,
      body: bodyObj,
    });

    const controller = new AbortController();
    const timeoutTimer = setTimeout(() => {
      controller.abort();
    }, this.config.upstreamTimeoutMs);

    if (options.signal) {
      options.signal.addEventListener("abort", () => controller.abort());
    }

    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers,
        body: bodyStr,
        signal: controller.signal,
      });
    } catch (err: unknown) {
      clearTimeout(timeoutTimer);
      if (controller.signal.aborted && !options.signal?.aborted) {
        throw new UpstreamTimeoutError(`Upstream provider timed out after ${this.config.upstreamTimeoutMs}ms`, err);
      }
      throw new UpstreamError(`Failed to connect to upstream provider: ${(err as Error).message}`, 502, err);
    } finally {
      clearTimeout(timeoutTimer);
    }

    const respHeaders: Record<string, string> = {};
    response.headers.forEach((val, key) => {
      respHeaders[key] = val;
    });

    let rawText: string;
    try {
      rawText = await response.text();
    } catch (err) {
      throw new UpstreamError("Failed to read response body from upstream", 502, err);
    }

    this.logger.debug({
      requestId: meta.requestId,
      sessionId: meta.sessionId,
      provider: this.providerName,
      model: this.modelName,
      direction: "upstream_to_proxy",
      stage: "complete_response",
      status: response.status,
      headers: respHeaders,
      rawText,
    });

    if (!response.ok) {
      throw new UpstreamError(`Upstream provider returned HTTP ${response.status}: ${rawText.slice(0, 300)}`, 502);
    }

    let parsed: any;
    try {
      parsed = JSON.parse(rawText);
    } catch (err) {
      throw new UpstreamError("Malformed JSON response from upstream provider", 502, err);
    }

    const choice = parsed.choices?.[0];
    if (!choice || !choice.message) {
      throw new UpstreamError("Upstream response missing choices[0].message", 502);
    }

    // Protocol check: Reject native tool calls
    if (choice.message.tool_calls && choice.message.tool_calls.length > 0) {
      throw new UpstreamError("Upstream returned unexpected native tool_calls", 502);
    }
    if (choice.message.function_call) {
      throw new UpstreamError("Upstream returned unexpected native function_call", 502);
    }

    const content = choice.message.content;
    if (typeof content !== "string") {
      throw new UpstreamError("Upstream message.content must be a string", 502);
    }

    return {
      content,
      finishReason: choice.finish_reason,
      usage: parsed.usage,
    };
  }

  public async stream(options: ProviderStreamOptions, meta: RequestMeta): Promise<AsyncIterable<ProviderStreamEvent>> {
    const url = this.getEndpointUrl();
    const headers = {
      ...this.getRequestHeaders(),
      Accept: "text/event-stream",
    };
    const bodyObj = this.buildRequestBody(options.messages, true);
    const bodyStr = JSON.stringify(bodyObj);

    this.logger.debug({
      requestId: meta.requestId,
      sessionId: meta.sessionId,
      provider: this.providerName,
      model: this.modelName,
      direction: "proxy_to_upstream",
      stage: "stream_request",
      url,
      method: "POST",
      headers,
      body: bodyObj,
    });

    const controller = new AbortController();
    const totalTimer = setTimeout(() => {
      controller.abort();
    }, this.config.upstreamTimeoutMs);

    if (options.signal) {
      options.signal.addEventListener("abort", () => controller.abort());
    }

    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers,
        body: bodyStr,
        signal: controller.signal,
      });
    } catch (err: unknown) {
      clearTimeout(totalTimer);
      if (controller.signal.aborted && !options.signal?.aborted) {
        throw new UpstreamTimeoutError(`Upstream provider timed out after ${this.config.upstreamTimeoutMs}ms`, err);
      }
      throw new UpstreamError(`Failed to connect to upstream provider: ${(err as Error).message}`, 502, err);
    }

    const respHeaders: Record<string, string> = {};
    response.headers.forEach((val, key) => {
      respHeaders[key] = val;
    });

    if (!response.ok) {
      clearTimeout(totalTimer);
      const errText = await response.text().catch(() => "");
      this.logger.debug({
        requestId: meta.requestId,
        sessionId: meta.sessionId,
        provider: this.providerName,
        model: this.modelName,
        direction: "upstream_to_proxy",
        stage: "stream_error_status",
        status: response.status,
        headers: respHeaders,
        rawText: errText,
      });
      throw new UpstreamError(`Upstream provider returned HTTP ${response.status}: ${errText.slice(0, 300)}`, 502);
    }

    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("text/event-stream")) {
      clearTimeout(totalTimer);
      throw new UpstreamError(`Upstream response is not text/event-stream (got '${contentType}')`, 502);
    }

    if (!response.body) {
      clearTimeout(totalTimer);
      throw new UpstreamError("Upstream streaming response has no body", 502);
    }

    this.logger.debug({
      requestId: meta.requestId,
      sessionId: meta.sessionId,
      provider: this.providerName,
      model: this.modelName,
      direction: "upstream_to_proxy",
      stage: "stream_headers_received",
      status: response.status,
      headers: respHeaders,
    });

    return this.createStreamGenerator(
      response.body as unknown as AsyncIterable<Uint8Array>,
      meta,
      controller,
      totalTimer,
      options.signal
    );
  }

  private async *createStreamGenerator(
    bodyStream: AsyncIterable<Uint8Array>,
    meta: RequestMeta,
    controller: AbortController,
    totalTimer: NodeJS.Timeout,
    clientSignal?: AbortSignal
  ): AsyncIterable<ProviderStreamEvent> {
    let idleTimer: NodeJS.Timeout | undefined;

    const resetIdleTimer = () => {
      if (idleTimer) clearTimeout(idleTimer);
      if (this.config.streamIdleTimeoutMs > 0) {
        idleTimer = setTimeout(() => {
          controller.abort();
        }, this.config.streamIdleTimeoutMs);
      }
    };

    resetIdleTimer();

    try {
      const sseEvents = parseSSEStream(bodyStream, {
        maxEventBytes: this.config.maxSseEventBytes,
        maxTotalBytes: this.config.maxUpstreamWireBytes,
      });

      let sawStopFinish = false;
      let sawDoneMarker = false;

      for await (const event of sseEvents) {
        resetIdleTimer();

        this.logger.debug({
          requestId: meta.requestId,
          sessionId: meta.sessionId,
          provider: this.providerName,
          model: this.modelName,
          direction: "upstream_to_proxy",
          stage: "sse_chunk",
          rawText: event.data,
        });

        if (event.data === "[DONE]") {
          sawDoneMarker = true;
          break;
        }

        let parsed: any;
        try {
          parsed = JSON.parse(event.data);
        } catch {
          // Ignore non-JSON or heartbeat SSE event
          continue;
        }

        const choice = parsed.choices?.[0];
        if (!choice) continue;

        // Protocol check: Reject native tool calls in delta
        if (choice.delta?.tool_calls && choice.delta.tool_calls.length > 0) {
          throw new UpstreamError("Upstream returned unexpected native tool_calls in stream delta", 502);
        }
        if (choice.delta?.function_call) {
          throw new UpstreamError("Upstream returned unexpected native function_call in stream delta", 502);
        }

        if (choice.finish_reason) {
          if (choice.finish_reason === "tool_calls" || choice.finish_reason === "function_call") {
            throw new UpstreamError("Upstream finished with unexpected tool_calls reason", 502);
          }
          if (choice.finish_reason === "stop") {
            sawStopFinish = true;
          }
        }

        const deltaContent = choice.delta?.content;
        if (typeof deltaContent === "string" && deltaContent.length > 0) {
          yield {
            delta: deltaContent,
            finishReason: choice.finish_reason,
            rawEvent: parsed,
          };
        }
      }

      // Check terminal sequence: Default compatibility profile requires stop finish or [DONE]
      if (!sawStopFinish && !sawDoneMarker) {
        throw new UpstreamError("Upstream stream ended abruptly without stop finish_reason or [DONE]", 502);
      }
    } catch (err: unknown) {
      if (controller.signal.aborted && !clientSignal?.aborted) {
        throw new UpstreamTimeoutError(`Upstream provider idle timed out after ${this.config.streamIdleTimeoutMs}ms`, err);
      }
      throw err;
    } finally {
      clearTimeout(totalTimer);
      if (idleTimer) clearTimeout(idleTimer);
    }
  }
}
