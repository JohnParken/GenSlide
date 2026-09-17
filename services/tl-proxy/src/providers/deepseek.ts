/**
 * DeepSeek provider adapter
 */

import { BaseOpenAICompatibleProvider } from "./openai-compatible.js";

export class DeepSeekProvider extends BaseOpenAICompatibleProvider {
  readonly providerName = "deepseek";

  protected getExtraPayloadFields(): Record<string, unknown> {
    const extra: Record<string, unknown> = {};
    if (this.config.upstreamThinking === "enabled") {
      extra.thinking = { type: "enabled" };
    } else if (this.config.upstreamThinking === "disabled") {
      extra.thinking = { type: "disabled" };
    }
    return extra;
  }
}
