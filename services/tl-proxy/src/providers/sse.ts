/**
 * Incremental SSE decoder with UTF-8 state preservation and size limits
 */

import { PayloadTooLargeError } from "../errors.js";

export interface ParsedSSEEvent {
  event?: string;
  data: string;
  id?: string;
}

export interface SSEParserOptions {
  maxEventBytes?: number;
  maxTotalBytes?: number;
}

export async function* parseSSEStream(
  stream: AsyncIterable<Uint8Array>,
  options: SSEParserOptions = {}
): AsyncIterable<ParsedSSEEvent> {
  const maxEventBytes = options.maxEventBytes ?? 262144; // 256 KiB
  const maxTotalBytes = options.maxTotalBytes ?? 67108864; // 64 MiB

  const decoder = new TextDecoder("utf-8", { fatal: false });
  let buffer = "";
  let totalBytes = 0;
  let currentEventBytes = 0;

  let currentEvent: string | undefined;
  let currentId: string | undefined;
  const currentDataLines: string[] = [];

  for await (const chunk of stream) {
    totalBytes += chunk.byteLength;
    if (totalBytes > maxTotalBytes) {
      throw new PayloadTooLargeError(`Upstream wire response exceeded maximum allowed bytes: ${maxTotalBytes}`, 502);
    }

    const text = decoder.decode(chunk, { stream: true });
    buffer += text;

    let newlineIndex: number;
    while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
      let line = buffer.slice(0, newlineIndex);
      buffer = buffer.slice(newlineIndex + 1);

      if (line.endsWith("\r")) {
        line = line.slice(0, -1);
      }

      const lineByteLength = Buffer.byteLength(line, "utf-8");
      currentEventBytes += lineByteLength;
      if (currentEventBytes > maxEventBytes) {
        throw new PayloadTooLargeError(`Upstream SSE event exceeded maximum allowed bytes: ${maxEventBytes}`, 502);
      }

      // Empty line indicates event boundary
      if (line === "") {
        if (currentDataLines.length > 0 || currentEvent !== undefined || currentId !== undefined) {
          const data = currentDataLines.join("\n");
          yield {
            event: currentEvent,
            id: currentId,
            data,
          };
        }
        currentEvent = undefined;
        currentId = undefined;
        currentDataLines.length = 0;
        currentEventBytes = 0;
        continue;
      }

      // Comment line
      if (line.startsWith(":")) {
        continue;
      }

      const colonIndex = line.indexOf(":");
      let field: string;
      let value = "";
      if (colonIndex === -1) {
        field = line;
      } else {
        field = line.slice(0, colonIndex);
        value = line.slice(colonIndex + 1);
        if (value.startsWith(" ")) {
          value = value.slice(1);
        }
      }

      if (field === "event") {
        currentEvent = value;
      } else if (field === "data") {
        currentDataLines.push(value);
      } else if (field === "id") {
        currentId = value;
      }
    }
  }

  // Flush any remaining trailing event if buffer has data
  if (buffer.length > 0) {
    let line = buffer;
    if (line.endsWith("\r")) {
      line = line.slice(0, -1);
    }
    if (line.startsWith("data:")) {
      let val = line.slice(5);
      if (val.startsWith(" ")) val = val.slice(1);
      currentDataLines.push(val);
    }
    if (currentDataLines.length > 0) {
      yield {
        event: currentEvent,
        id: currentId,
        data: currentDataLines.join("\n"),
      };
    }
  }
}
