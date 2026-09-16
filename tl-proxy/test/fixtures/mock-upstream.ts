/**
 * Mock HTTP upstream server for offline testing
 */

import http from "node:http";
import { AddressInfo } from "node:net";

export interface RecordedRequest {
  method: string;
  url: string;
  headers: http.IncomingHttpHeaders;
  body: any;
}

export type MockHandler = (
  req: http.IncomingMessage,
  res: http.ServerResponse,
  body: any
) => void | Promise<void>;

export class MockUpstream {
  private server: http.Server;
  public recordedRequests: RecordedRequest[] = [];
  public customHandler?: MockHandler;

  constructor() {
    this.server = http.createServer(async (req, res) => {
      const chunks: Buffer[] = [];
      for await (const chunk of req) {
        chunks.push(chunk as Buffer);
      }
      const rawBody = Buffer.concat(chunks).toString("utf-8");
      let body: any;
      try {
        body = JSON.parse(rawBody);
      } catch {
        body = rawBody;
      }

      this.recordedRequests.push({
        method: req.method || "GET",
        url: req.url || "/",
        headers: req.headers,
        body,
      });

      if (this.customHandler) {
        await this.customHandler(req, res, body);
        return;
      }

      // Default mock completion
      if (body?.stream) {
        res.writeHead(200, {
          "Content-Type": "text/event-stream; charset=utf-8",
          "Cache-Control": "no-cache",
        });
        res.write(
          `data: ${JSON.stringify({
            choices: [{ delta: { content: "Hello " } }],
          })}\n\n`
        );
        res.write(
          `data: ${JSON.stringify({
            choices: [{ delta: { content: "World!" }, finish_reason: "stop" }],
          })}\n\n`
        );
        res.write("data: [DONE]\n\n");
        res.end();
      } else {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            id: "chatcmpl-mock",
            choices: [
              {
                index: 0,
                message: {
                  role: "assistant",
                  content: "Mock completion response",
                },
                finish_reason: "stop",
              },
            ],
          })
        );
      }
    });
  }

  public async start(): Promise<string> {
    return new Promise((resolve) => {
      this.server.listen(0, "127.0.0.1", () => {
        const addr = this.server.address() as AddressInfo;
        resolve(`http://127.0.0.1:${addr.port}`);
      });
    });
  }

  public async close(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.server.close((err) => (err ? reject(err) : resolve()));
    });
  }

  public clear(): void {
    this.recordedRequests = [];
    this.customHandler = undefined;
  }
}
