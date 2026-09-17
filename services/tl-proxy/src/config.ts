/**
 * Proxy configuration parser and validation
 */

export type LogLevel = "debug" | "info" | "warn" | "error";
export type AuthMode = "local" | "bearer";
export type ThinkingMode = "provider-default" | "enabled" | "disabled";

export interface ProxyConfig {
  host: string;
  port: number;
  upstreamProvider: string;
  upstreamModel: string;
  upstreamBaseUrl: string;
  upstreamApiKey: string;
  systemPromptVariableName: string;
  legacyRoleText: boolean;
  logLevel: LogLevel;
  upstreamMaxTokens?: number;
  upstreamThinking: ThinkingMode;
  upstreamTimeoutMs: number;
  streamIdleTimeoutMs: number;
  maxRequestBytes: number;
  maxResponseBytes: number;
  maxUpstreamWireBytes: number;
  maxDownstreamWireBytes: number;
  maxSseEventBytes: number;
  sessionTtlMs: number;
  maxSessions: number;
  maxInflightRequests: number;
  authMode: AuthMode;
  testAccessToken?: string;
  corsAllowedOrigins: string[];
}

function parseNumber(val: string | undefined, defaultValue: number): number {
  if (!val) return defaultValue;
  const parsed = Number(val);
  return Number.isFinite(parsed) ? parsed : defaultValue;
}

function parseBoolean(val: string | undefined, defaultValue: boolean): boolean {
  if (val === undefined) return defaultValue;
  const lower = val.trim().toLowerCase();
  return lower === "true" || lower === "1" || lower === "yes";
}

export function loadConfigFromEnv(env: Record<string, string | undefined> = process.env): ProxyConfig {
  const host = env.TL_PROXY_HOST || "127.0.0.1";
  const port = parseNumber(env.TL_PROXY_PORT, 8089);
  const upstreamProvider = (env.UPSTREAM_PROVIDER || "qwen").toLowerCase();
  const upstreamModel = env.UPSTREAM_MODEL || (upstreamProvider === "qwen" ? "qwen3.8-flash" : "deepseek-v4-flash");
  const upstreamBaseUrl = (env.UPSTREAM_BASE_URL || "").trim();
  const upstreamApiKey = (env.UPSTREAM_API_KEY || "").trim();
  const systemPromptVariableName = (env.SYSTEM_PROMPT_VARIABLE_NAME || "system_prompt").trim();
  const legacyRoleText = parseBoolean(env.LEGACY_ROLE_TEXT, true);
  
  const rawLogLevel = (env.LOG_LEVEL || "debug").toLowerCase();
  const logLevel: LogLevel = ["debug", "info", "warn", "error"].includes(rawLogLevel)
    ? (rawLogLevel as LogLevel)
    : "debug";

  const upstreamMaxTokens = env.UPSTREAM_MAX_TOKENS ? Number(env.UPSTREAM_MAX_TOKENS) : undefined;
  
  const rawThinking = (env.UPSTREAM_THINKING || "provider-default").toLowerCase();
  const upstreamThinking: ThinkingMode = ["provider-default", "enabled", "disabled"].includes(rawThinking)
    ? (rawThinking as ThinkingMode)
    : "provider-default";

  const upstreamTimeoutMs = parseNumber(env.UPSTREAM_TIMEOUT_MS, 120000);
  const streamIdleTimeoutMs = parseNumber(env.STREAM_IDLE_TIMEOUT_MS, 30000);
  const maxRequestBytes = parseNumber(env.MAX_REQUEST_BYTES, 1048576); // 1 MiB
  const maxResponseBytes = parseNumber(env.MAX_RESPONSE_BYTES, 4194304); // 4 MiB
  const maxUpstreamWireBytes = parseNumber(env.MAX_UPSTREAM_WIRE_BYTES, 67108864); // 64 MiB
  const maxDownstreamWireBytes = parseNumber(env.MAX_DOWNSTREAM_WIRE_BYTES, 67108864); // 64 MiB
  const maxSseEventBytes = parseNumber(env.MAX_SSE_EVENT_BYTES, 262144); // 256 KiB
  const sessionTtlMs = parseNumber(env.SESSION_TTL_MS, 900000); // 15 min
  const maxSessions = parseNumber(env.MAX_SESSIONS, 10000);
  const maxInflightRequests = parseNumber(env.MAX_INFLIGHT_REQUESTS, 32);

  const rawAuthMode = (env.AUTH_MODE || "local").toLowerCase();
  const authMode: AuthMode = rawAuthMode === "bearer" ? "bearer" : "local";
  const testAccessToken = env.TEST_ACCESS_TOKEN ? env.TEST_ACCESS_TOKEN.trim() : undefined;

  const corsAllowedOrigins = env.CORS_ALLOWED_ORIGINS
    ? env.CORS_ALLOWED_ORIGINS.split(",").map((s) => s.trim()).filter(Boolean)
    : [];

  return {
    host,
    port,
    upstreamProvider,
    upstreamModel,
    upstreamBaseUrl,
    upstreamApiKey,
    systemPromptVariableName,
    legacyRoleText,
    logLevel,
    upstreamMaxTokens,
    upstreamThinking,
    upstreamTimeoutMs,
    streamIdleTimeoutMs,
    maxRequestBytes,
    maxResponseBytes,
    maxUpstreamWireBytes,
    maxDownstreamWireBytes,
    maxSseEventBytes,
    sessionTtlMs,
    maxSessions,
    maxInflightRequests,
    authMode,
    testAccessToken,
    corsAllowedOrigins,
  };
}

export function validateConfig(config: ProxyConfig, options: { requireUpstreamKey?: boolean } = {}): void {
  if (config.port < 0 || config.port > 65535) {
    throw new Error(`Invalid TL_PROXY_PORT: ${config.port}`);
  }
  if (!config.systemPromptVariableName) {
    throw new Error("SYSTEM_PROMPT_VARIABLE_NAME must not be empty");
  }
  if (config.authMode === "bearer" && !config.testAccessToken) {
    throw new Error("TEST_ACCESS_TOKEN is required when AUTH_MODE=bearer");
  }
  if (options.requireUpstreamKey && !config.upstreamApiKey) {
    throw new Error("UPSTREAM_API_KEY is required for live upstream calls");
  }
}
