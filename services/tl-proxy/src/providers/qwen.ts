/**
 * Qwen provider adapter (qwen3.8-flash)
 */

import { BaseOpenAICompatibleProvider } from "./openai-compatible.js";

export class QwenProvider extends BaseOpenAICompatibleProvider {
  readonly providerName = "qwen";

  protected getExtraPayloadFields(): Record<string, unknown> {
    const extra: Record<string, unknown> = {};
    if (this.config.upstreamThinking === "enabled") {
      extra.enable_thinking = true;
    } else if (this.config.upstreamThinking === "disabled") {
      extra.enable_thinking = false;
    }
    return extra;
  }
}
