/**
 * TL Proxy HTTP Server
 */

import http from "node:http";
import { AddressInfo } from "node:net";
import { ProxyConfig } from "./config.js";
import { AuthError, PayloadTooLargeError, ProtocolError, ProxyError, SessionError } from "./errors.js";
import { ProxyLogger } from "./logger.js";
import {
  buildChatResponse,
  buildErrorResponse,
  buildInitSessionResponse,
  formatSSEChunk,
  formatSSEDone,
  formatSSEError,
  parseChatData,
  parseInitSessionData,
  parseLegacyRoleMessages,
  validateEnvelope,
} from "./protocol.js";
import { DeepSeekProvider } from "./providers/deepseek.js";
import { QwenProvider } from "./providers/qwen.js";
import { ProviderAdapter, ProviderChatMessage } from "./providers/types.js";
import { SessionStore } from "./session-store.js";

export interface ProxyServerOptions {
  config: ProxyConfig;
  logger?: ProxyLogger;
  provider?: ProviderAdapter;
  sessionStore?: SessionStore;
}

export class ProxyServer {
  public readonly config: ProxyConfig;
  public readonly logger: ProxyLogger;
  public readonly provider: ProviderAdapter;
  public readonly sessionStore: SessionStore;

  private server: http.Server;
  private inflightRequests = 0;
  private isClosing = false;
  private activeSockets = new Set<import("node:net").Socket>();

  constructor(options: ProxyServerOptions) {
    this.config = options.config;

    this.logger =
      options.logger ||
      new ProxyLogger({
        level: this.config.logLevel,
        sensitiveTokens: [this.config.upstreamApiKey, this.config.testAccessToken || ""].filter(Boolean),
      });

    this.sessionStore =
      options.sessionStore ||
      new SessionStore({
        ttlMs: this.config.sessionTtlMs,
        maxSessions: this.config.maxSessions,
      });

    if (options.provider) {
      this.provider = options.provider;
    } else if (this.config.upstreamProvider === "deepseek") {
      this.provider = new DeepSeekProvider(this.config, this.logger);
    } else {
      this.provider = new QwenProvider(this.config, this.logger);
    }

    this.server = http.createServer((req, res) => {
      this.handleRequest(req, res).catch((err) => {
        this.logger.error({
          requestId: (req as any).requestId || "unknown",
          direction: "proxy_to_client",
          stage: "unhandled_request_error",
          rawText: String(err),
        });
        if (!res.headersSent) {
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify(buildErrorResponse("Internal server error")));
        }
      });
    });

    this.server.on("connection", (socket) => {
      this.activeSockets.add(socket);
      socket.on("close", () => {
        this.activeSockets.delete(socket);
      });
    });
  }

  public async start(): Promise<string> {
    return new Promise((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(this.config.port, this.config.host, () => {
        const addr = this.server.address() as AddressInfo;
        const url = `http://${this.config.host}:${addr.port}`;
        resolve(url);
      });
    });
  }

  public address(): AddressInfo | null {
    return this.server.address() as AddressInfo | null;
  }

  public async close(): Promise<void> {
    this.isClosing = true;
    this.sessionStore.close();

    return new Promise((resolve, reject) => {
      // Destroy active sockets if any
      for (const socket of this.activeSockets) {
        socket.destroy();
      }
      this.activeSockets.clear();

      this.server.close((err) => {
        if (err) reject(err);
        else resolve();
      });
    });
  }

  private handleCors(req: http.IncomingMessage, res: http.ServerResponse): boolean {
    const origin = req.headers.origin;
    if (!origin) return false;

    if (
      this.config.corsAllowedOrigins.includes("*") ||
      this.config.corsAllowedOrigins.includes(origin)
    ) {
      res.setHeader("Access-Control-Allow-Origin", origin);
      res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
      res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Request-Id, Accept");
      res.setHeader("Access-Control-Max-Age", "86400");
    }

    if (req.method === "OPTIONS") {
      res.writeHead(204);
      res.end();
      return true;
    }

    return false;
  }

  private checkAuth(req: http.IncomingMessage): void {
    if (this.config.authMode === "local") {
      return;
    }

    if (this.config.authMode === "bearer") {
      const authHeader = req.headers.authorization;
      if (!authHeader || !authHeader.startsWith("Bearer ")) {
        throw new AuthError("Missing or invalid Bearer token");
      }
      const token = authHeader.slice(7).trim();
      if (token !== this.config.testAccessToken) {
        throw new AuthError("Forbidden: invalid test access token", 403);
      }
    }
  }

  private async readBody(req: http.IncomingMessage): Promise<Buffer> {
    const chunks: Buffer[] = [];
    let bytesReceived = 0;

    return new Promise((resolve, reject) => {
      req.on("data", (chunk: Buffer) => {
        bytesReceived += chunk.length;
        if (bytesReceived > this.config.maxRequestBytes) {
          reject(new PayloadTooLargeError(`Request body exceeded ${this.config.maxRequestBytes} bytes`));
          return;
        }
        chunks.push(chunk);
      });

      req.on("end", () => {
        resolve(Buffer.concat(chunks));
      });

      req.on("error", reject);
    });
  }

  private async handleRequest(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    if (this.isClosing) {
      res.writeHead(503, { "Content-Type": "application/json" });
      res.end(JSON.stringify(buildErrorResponse("Server is shutting down")));
      return;
    }

    if (this.handleCors(req, res)) {
      return;
    }

    if (this.inflightRequests >= this.config.maxInflightRequests) {
      res.writeHead(503, { "Content-Type": "application/json" });
      res.end(JSON.stringify(buildErrorResponse("Too many concurrent requests")));
      return;
    }

    this.inflightRequests++;
    try {
      const urlPath = (req.url || "/").split("?")[0];

      if (req.method !== "POST") {
        res.setHeader("Allow", "POST, OPTIONS");
        res.writeHead(405, { "Content-Type": "application/json" });
        res.end(JSON.stringify(buildErrorResponse("Method Not Allowed")));
        return;
      }

      this.checkAuth(req);

      if (urlPath === "/chatbbc/init_session") {
        await this.handleInitSession(req, res);
      } else if (urlPath === "/chatbbc/chat") {
        await this.handleChat(req, res);
      } else {
        res.writeHead(404, { "Content-Type": "application/json" });
        res.end(JSON.stringify(buildErrorResponse("Not Found")));
      }
    } catch (err: unknown) {
      this.sendErrorResponse(res, err);
    } finally {
      this.inflightRequests--;
    }
  }

  private sendErrorResponse(res: http.ServerResponse, err: unknown): void {
    if (res.headersSent) {
      // If headers already sent, we cannot send JSON error
      res.destroy();
      return;
    }

    let status = 500;
    let safeMessage = "Internal Server Error";

    if (err instanceof ProxyError) {
      status = err.statusCode;
      safeMessage = err.safeMessage;
    } else if (err instanceof Error) {
      safeMessage = err.message;
    }

    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(buildErrorResponse(safeMessage)));
  }

  private async handleInitSession(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    const rawBuffer = await this.readBody(req);
    let rawJson: unknown;
    try {
      rawJson = JSON.parse(rawBuffer.toString("utf-8"));
    } catch {
      throw new ProtocolError("Malformed JSON in request body");
    }

    const envelope = validateEnvelope(rawJson);
    const requestId = envelope.requestId || `init_${Date.now()}`;
    (req as any).requestId = requestId;

    this.logger.debug({
      requestId,
      direction: "client_to_proxy",
      stage: "init_session_request",
      method: "POST",
      url: req.url,
      headers: req.headers as Record<string, string>,
      body: rawJson,
    });

    const variables = parseInitSessionData(
      envelope.data,
      this.config.systemPromptVariableName,
      this.config.legacyRoleText
    );

    const session = this.sessionStore.createSession(variables);
    const responsePayload = buildInitSessionResponse(session.id);

    this.logger.debug({
      requestId,
      sessionId: session.id,
      direction: "proxy_to_client",
      stage: "init_session_response",
      status: 200,
      body: responsePayload,
    });

    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(responsePayload));
  }

  private async handleChat(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    const rawBuffer = await this.readBody(req);
    let rawJson: unknown;
    try {
      rawJson = JSON.parse(rawBuffer.toString("utf-8"));
    } catch {
      throw new ProtocolError("Malformed JSON in request body");
    }

    const envelope = validateEnvelope(rawJson);
    const requestId = envelope.requestId || `chat_${Date.now()}`;
    (req as any).requestId = requestId;

    this.logger.debug({
      requestId,
      direction: "client_to_proxy",
      stage: "chat_request",
      method: "POST",
      url: req.url,
      headers: req.headers as Record<string, string>,
      body: rawJson,
    });

    const chatData = parseChatData(envelope.data);
    const session = this.sessionStore.getSession(chatData.session_id);
    if (!session) {
      throw new SessionError("Session not found or expired", 404);
    }

    // Build messages
    const messages: ProviderChatMessage[] = [];
    const boundSystemPrompt = session.variables.get(this.config.systemPromptVariableName);

    if (boundSystemPrompt !== undefined && boundSystemPrompt.length > 0) {
      messages.push({
        role: "system",
        content: boundSystemPrompt,
      });
      messages.push({
        role: "user",
        content: chatData.txt,
      });
    } else if (this.config.legacyRoleText) {
      const parsed = parseLegacyRoleMessages(chatData.txt);
      for (const m of parsed) {
        messages.push({
          role: m.role as "system" | "user" | "assistant",
          content: m.content,
        });
      }
    } else {
      messages.push({
        role: "user",
        content: chatData.txt,
      });
    }

    const abortController = new AbortController();
    req.on("close", () => {
      if (!res.writableEnded) {
        abortController.abort();
      }
    });

    const meta = {
      requestId,
      sessionId: session.id,
    };

    if (!chatData.stream) {
      // Non-streaming
      const providerResp = await this.provider.complete(
        {
          messages,
          signal: abortController.signal,
        },
        meta
      );

      const respPayload = buildChatResponse(providerResp.content);
      const respStr = JSON.stringify(respPayload);

      this.logger.debug({
        requestId,
        sessionId: session.id,
        direction: "proxy_to_client",
        stage: "chat_response",
        status: 200,
        body: respPayload,
      });

      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(respStr);
    } else {
      // Streaming SSE
      let streamIter: AsyncIterable<import("./providers/types.js").ProviderStreamEvent>;
      try {
        streamIter = await this.provider.stream(
          {
            messages,
            signal: abortController.signal,
          },
          meta
        );
      } catch (err: unknown) {
        // If upstream fails before stream starts (before headers sent)
        this.sendErrorResponse(res, err);
        return;
      }

      // Headers sent downstream
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
      });
      if (typeof (res as any).flushHeaders === "function") {
        (res as any).flushHeaders();
      }

      let frameSeq = 0;
      let totalDownstreamWireBytes = 0;
      let totalResponseBytes = 0;

      try {
        for await (const chunk of streamIter) {
          if (chunk.delta) {
            const deltaBytes = Buffer.byteLength(chunk.delta, "utf-8");
            totalResponseBytes += deltaBytes;
            if (totalResponseBytes > this.config.maxResponseBytes) {
              throw new PayloadTooLargeError("Stream response exceeded maxResponseBytes");
            }

            const frame = formatSSEChunk(chunk.delta, `${requestId}-${++frameSeq}`);
            const frameBytes = Buffer.byteLength(frame, "utf-8");
            totalDownstreamWireBytes += frameBytes;

            if (totalDownstreamWireBytes > this.config.maxDownstreamWireBytes) {
              throw new PayloadTooLargeError("Stream response exceeded maxDownstreamWireBytes");
            }

            this.logger.debug({
              requestId,
              sessionId: session.id,
              direction: "proxy_to_client",
              stage: "sse_chunk",
              rawText: frame,
            });

            const ok = res.write(frame);
            if (!ok) {
              await new Promise((r) => res.once("drain", r));
            }
          }
        }

        // Terminal done event
        const doneFrame = formatSSEDone(`${requestId}-${++frameSeq}`);
        this.logger.debug({
          requestId,
          sessionId: session.id,
          direction: "proxy_to_client",
          stage: "sse_done",
          rawText: doneFrame,
        });

        res.write(doneFrame);
        res.end();
      } catch (err: unknown) {
        const errorMsg = err instanceof ProxyError ? err.safeMessage : "Upstream stream error";
        const errFrame = formatSSEError(errorMsg);

        this.logger.error({
          requestId,
          sessionId: session.id,
          direction: "proxy_to_client",
          stage: "sse_error",
          rawText: errFrame,
        });

        // Write single error event and destroy socket
        res.write(errFrame);
        res.destroy();
      }
    }
  }
}

export function createProxy(config: ProxyConfig, options?: Partial<ProxyServerOptions>): ProxyServer {
  return new ProxyServer({
    config,
    ...options,
  });
}
