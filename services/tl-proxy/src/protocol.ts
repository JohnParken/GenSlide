/**
 * TL / ChatBBC protocol validation, envelope parsing, and response formatting
 */

import { ProtocolError } from "./errors.js";

export interface TLEnvelope<T = unknown> {
  appId?: string;
  trCode?: string;
  trVersion?: string;
  timestamp?: number;
  requestId?: string;
  data: T;
}

export interface PromptVariable {
  name: string;
  value: string;
}

export interface InitSessionRequestData {
  prompt_variables?: PromptVariable[];
}

export interface ChatFile {
  file_id?: string;
  url?: string;
  content_type?: string;
}

export interface ChatRequestData {
  session_id: string;
  txt: string;
  files?: ChatFile[];
  stream?: boolean;
}

export interface TLSuccessResponse<T = unknown> {
  code: 0;
  message: "success";
  data: T;
}

export interface TLErrorResponse {
  error: string;
}

export const UNSUPPORTED_TOOL_FIELDS = [
  "tools",
  "tool_choice",
  "functions",
  "function_call",
  "parallel_tool_calls",
  "tool_calls",
];

export function validateEnvelope<T = unknown>(rawBody: unknown): TLEnvelope<T> {
  if (typeof rawBody !== "object" || rawBody === null || Array.isArray(rawBody)) {
    throw new ProtocolError("Request body must be a JSON object");
  }

  const obj = rawBody as Record<string, unknown>;

  // Check for forbidden native tool fields at root level
  for (const field of UNSUPPORTED_TOOL_FIELDS) {
    if (field in obj && obj[field] !== undefined) {
      throw new ProtocolError(`Unsupported native tool field: '${field}' at root level`);
    }
  }

  if (obj.appId !== undefined && typeof obj.appId !== "string") {
    throw new ProtocolError("Field 'appId' must be a string");
  }
  if (obj.trCode !== undefined && typeof obj.trCode !== "string") {
    throw new ProtocolError("Field 'trCode' must be a string");
  }
  if (obj.trVersion !== undefined && typeof obj.trVersion !== "string") {
    throw new ProtocolError("Field 'trVersion' must be a string");
  }
  if (obj.timestamp !== undefined && typeof obj.timestamp !== "number") {
    throw new ProtocolError("Field 'timestamp' must be a number");
  }
  if (obj.requestId !== undefined && typeof obj.requestId !== "string") {
    throw new ProtocolError("Field 'requestId' must be a string");
  }
  if (!obj.data || typeof obj.data !== "object" || Array.isArray(obj.data)) {
    throw new ProtocolError("Field 'data' must be a JSON object");
  }

  // Check forbidden native tool fields at data level
  const dataObj = obj.data as Record<string, unknown>;
  for (const field of UNSUPPORTED_TOOL_FIELDS) {
    if (field in dataObj && dataObj[field] !== undefined) {
      throw new ProtocolError(`Unsupported native tool field: '${field}' at data level`);
    }
  }

  return {
    appId: obj.appId as string | undefined,
    trCode: obj.trCode as string | undefined,
    trVersion: obj.trVersion as string | undefined,
    timestamp: obj.timestamp as number | undefined,
    requestId: (obj.requestId as string) || `req_${Date.now()}`,
    data: obj.data as T,
  };
}

export function parseInitSessionData(
  data: unknown,
  systemPromptVarName: string,
  legacyRoleText: boolean
): Map<string, string> {
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new ProtocolError("Init session data must be an object");
  }

  const obj = data as Record<string, unknown>;
  const variables = new Map<string, string>();

  if (obj.prompt_variables === undefined) {
    return variables;
  }

  if (!Array.isArray(obj.prompt_variables)) {
    throw new ProtocolError("Field 'prompt_variables' must be an array");
  }

  const seenNames = new Set<string>();

  for (let i = 0; i < obj.prompt_variables.length; i++) {
    const item = obj.prompt_variables[i];
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      throw new ProtocolError(`prompt_variables[${i}] must be an object`);
    }

    const name = item.name;
    const value = item.value;

    if (typeof name !== "string" || name.trim() === "") {
      throw new ProtocolError(`prompt_variables[${i}].name must be a non-empty string`);
    }

    if (seenNames.has(name)) {
      throw new ProtocolError(`Duplicate variable name '${name}' in prompt_variables`);
    }
    seenNames.add(name);

    if (typeof value !== "string") {
      // Legacy compatibility: accept name-only variable with undefined/empty value if enabled
      if (legacyRoleText && value === undefined) {
        variables.set(name, "");
        continue;
      }
      throw new ProtocolError(`prompt_variables[${i}].value must be a string`);
    }

    variables.set(name, value);
  }

  // If variables are present other than name-only, systemPromptVarName should be non-empty string
  if (variables.size > 0) {
    const sysPrompt = variables.get(systemPromptVarName);
    // If sysPrompt is present, it must not be blank whitespace-only
    if (sysPrompt !== undefined && sysPrompt.trim() === "") {
      throw new ProtocolError(`Configured system prompt variable '${systemPromptVarName}' cannot be empty`);
    }
  }

  return variables;
}

export function parseChatData(data: unknown): ChatRequestData {
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    throw new ProtocolError("Chat data must be an object");
  }

  const obj = data as Record<string, unknown>;

  if (typeof obj.session_id !== "string" || obj.session_id.trim() === "") {
    throw new ProtocolError("Field 'session_id' must be a non-empty string");
  }

  if (typeof obj.txt !== "string") {
    throw new ProtocolError("Field 'txt' must be a string");
  }

  let stream = true;
  if (obj.stream !== undefined) {
    if (typeof obj.stream !== "boolean") {
      throw new ProtocolError("Field 'stream' must be a boolean (true/false)");
    }
    stream = obj.stream;
  }

  // Validate files
  if (obj.files !== undefined) {
    if (!Array.isArray(obj.files)) {
      throw new ProtocolError("Field 'files' must be an array");
    }
    for (const f of obj.files) {
      if (typeof f !== "object" || f === null) {
        throw new ProtocolError("Each file item must be an object");
      }
      const fileObj = f as Record<string, unknown>;
      const fileId = fileObj.file_id;
      const url = fileObj.url;
      // If either file_id or url has a non-empty string, reject with 400
      if ((typeof fileId === "string" && fileId.trim() !== "") || (typeof url === "string" && url.trim() !== "")) {
        throw new ProtocolError("Real file attachments are not supported by this proxy");
      }
    }
  }

  return {
    session_id: obj.session_id,
    txt: obj.txt,
    files: obj.files as ChatFile[] | undefined,
    stream,
  };
}

export function buildInitSessionResponse(sessionId: string): TLSuccessResponse<{ session_id: string }> {
  return {
    code: 0,
    message: "success",
    data: {
      session_id: sessionId,
    },
  };
}

export function buildChatResponse(txt: string): TLSuccessResponse<{ txt: string }> {
  return {
    code: 0,
    message: "success",
    data: {
      txt,
    },
  };
}

export function buildErrorResponse(safeMessage: string): TLErrorResponse {
  return {
    error: safeMessage,
  };
}

export function formatSSEChunk(content: string, id?: string): string {
  const payload = JSON.stringify({ content });
  const lines: string[] = [];
  if (id) {
    lines.push(`id: ${id}`);
  }
  lines.push("event: chunk");
  lines.push(`data: ${payload}`);
  lines.push("");
  lines.push("");
  return lines.join("\n");
}

export function formatSSEDone(id?: string): string {
  const payload = JSON.stringify({ finished: true });
  const lines: string[] = [];
  if (id) {
    lines.push(`id: ${id}`);
  }
  lines.push("event: done");
  lines.push(`data: ${payload}`);
  lines.push("");
  lines.push("");
  return lines.join("\n");
}

export function formatSSEError(message: string): string {
  const payload = JSON.stringify({ message });
  return `event: error\ndata: ${payload}\n\n`;
}

/**
 * Legacy parsing for role markers in txt when no system variable was bound
 */
export function parseLegacyRoleMessages(txt: string): Array<{ role: string; content: string }> {
  const lines = txt.split(/\r?\n/);
  const messages: Array<{ role: string; content: string }> = [];
  let currentRole = "user";
  let currentContent: string[] = [];

  const rolePrefixRegex = /^(system|user|assistant):\s*(.*)$/i;

  let hasMarkers = false;
  for (const line of lines) {
    if (rolePrefixRegex.test(line)) {
      hasMarkers = true;
      break;
    }
  }

  if (!hasMarkers) {
    return [{ role: "user", content: txt }];
  }

  for (const line of lines) {
    const match = line.match(rolePrefixRegex);
    if (match) {
      if (currentContent.length > 0 || messages.length > 0) {
        messages.push({
          role: currentRole,
          content: currentContent.join("\n").trim(),
        });
        currentContent = [];
      }
      currentRole = match[1].toLowerCase();
      if (match[2]) {
        currentContent.push(match[2]);
      }
    } else {
      currentContent.push(line);
    }
  }

  if (currentContent.length > 0) {
    messages.push({
      role: currentRole,
      content: currentContent.join("\n").trim(),
    });
  }

  return messages.filter((m) => m.content.length > 0);
}
