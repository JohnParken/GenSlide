/**
 * Four-direction structured debug logger with credential masking
 */

import { LogLevel } from "./config.js";

export type Direction =
  | "client_to_proxy"
  | "proxy_to_upstream"
  | "upstream_to_proxy"
  | "proxy_to_client";

export interface LogEvent {
  timestamp: string;
  requestId: string;
  sessionId?: string;
  provider?: string;
  model?: string;
  direction: Direction;
  sequence: number;
  stage: string;
  headers?: Record<string, string>;
  body?: unknown;
  status?: number;
  url?: string;
  method?: string;
  rawText?: string;
}

const SENSITIVE_HEADER_KEYS = new Set([
  "authorization",
  "proxy-authorization",
  "cookie",
  "set-cookie",
  "api-key",
  "x-api-key",
  "chatbbc-auth-token",
  "tl-auth-token",
]);

export interface LoggerOptions {
  level: LogLevel;
  sensitiveTokens?: string[];
  sink?: (entry: string) => void;
}

export class ProxyLogger {
  private level: LogLevel;
  private sensitiveTokens: string[];
  private seq = 0;
  private sink: (entry: string) => void;

  constructor(options: LoggerOptions) {
    this.level = options.level;
    this.sensitiveTokens = (options.sensitiveTokens || []).filter((t) => t && t.length > 0);
    this.sink = options.sink || ((str: string) => process.stdout.write(str + "\n"));
  }

  public nextSeq(): number {
    return ++this.seq;
  }

  public shouldLog(level: LogLevel): boolean {
    const priority: Record<LogLevel, number> = {
      debug: 0,
      info: 1,
      warn: 2,
      error: 3,
    };
    return priority[level] >= priority[this.level];
  }

  public maskString(text: string): string {
    if (!text || this.sensitiveTokens.length === 0) return text;
    let result = text;
    for (const token of this.sensitiveTokens) {
      if (token && result.includes(token)) {
        result = result.split(token).join("[REDACTED]");
      }
    }
    return result;
  }

  public maskHeaders(headers: Record<string, string | string[] | undefined>): Record<string, string> {
    const masked: Record<string, string> = {};
    for (const [key, val] of Object.entries(headers)) {
      if (val === undefined) continue;
      const lower = key.toLowerCase();
      const stringVal = Array.isArray(val) ? val.join(", ") : String(val);
      if (SENSITIVE_HEADER_KEYS.has(lower)) {
        masked[key] = "[REDACTED]";
      } else {
        masked[key] = this.maskString(stringVal);
      }
    }
    return masked;
  }

  public maskBody(body: unknown): unknown {
    if (typeof body === "string") {
      return this.maskString(body);
    }
    if (body === null || typeof body !== "object") {
      return body;
    }
    if (Array.isArray(body)) {
      return body.map((item) => this.maskBody(item));
    }
    const result: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(body as Record<string, unknown>)) {
      const lowerKey = k.toLowerCase();
      if (SENSITIVE_HEADER_KEYS.has(lowerKey) || lowerKey === "key" || lowerKey === "apikey" || lowerKey === "token") {
        result[k] = "[REDACTED]";
      } else {
        result[k] = this.maskBody(v);
      }
    }
    return result;
  }

  public log(level: LogLevel, event: Omit<LogEvent, "timestamp" | "sequence"> & { sequence?: number }): void {
    if (!this.shouldLog(level)) return;

    const fullEvent: LogEvent = {
      timestamp: new Date().toISOString(),
      sequence: event.sequence ?? this.nextSeq(),
      requestId: event.requestId,
      sessionId: event.sessionId,
      provider: event.provider,
      model: event.model,
      direction: event.direction,
      stage: event.stage,
      status: event.status,
      url: event.url,
      method: event.method,
      headers: event.headers ? this.maskHeaders(event.headers) : undefined,
      body: event.body !== undefined ? this.maskBody(event.body) : undefined,
      rawText: event.rawText !== undefined ? this.maskString(event.rawText) : undefined,
    };

    const formatted = JSON.stringify(fullEvent);
    this.sink(formatted);
  }

  public debug(event: Omit<LogEvent, "timestamp" | "sequence"> & { sequence?: number }): void {
    this.log("debug", event);
  }

  public info(event: Omit<LogEvent, "timestamp" | "sequence"> & { sequence?: number }): void {
    this.log("info", event);
  }

  public warn(event: Omit<LogEvent, "timestamp" | "sequence"> & { sequence?: number }): void {
    this.log("warn", event);
  }

  public error(event: Omit<LogEvent, "timestamp" | "sequence"> & { sequence?: number }): void {
    this.log("error", event);
  }
}
