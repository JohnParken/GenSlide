/**
 * Provider abstractions and event types
 */

export interface ProviderChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface RequestMeta {
  requestId: string;
  sessionId?: string;
}

export interface ProviderCompleteOptions {
  messages: ProviderChatMessage[];
  signal?: AbortSignal;
}

export interface ProviderStreamOptions {
  messages: ProviderChatMessage[];
  signal?: AbortSignal;
}

export interface ProviderResponse {
  content: string;
  finishReason?: string;
  usage?: unknown;
}

export interface ProviderStreamEvent {
  delta?: string;
  finishReason?: string;
  rawEvent?: unknown;
}

export interface ProviderAdapter {
  readonly providerName: string;
  readonly modelName: string;

  complete(options: ProviderCompleteOptions, meta: RequestMeta): Promise<ProviderResponse>;
  stream(options: ProviderStreamOptions, meta: RequestMeta): Promise<AsyncIterable<ProviderStreamEvent>>;
}
