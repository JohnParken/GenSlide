#!/usr/bin/env node

/**
 * Command line entry point for tl-proxy
 */

import { loadConfigFromEnv, validateConfig } from "./config.js";
import { createProxy } from "./server.js";

async function main() {
  if (typeof process.loadEnvFile === "function") {
    try {
      process.loadEnvFile();
    } catch {
      try {
        process.loadEnvFile(new URL("../../.env", import.meta.url));
      } catch {
        try {
          process.loadEnvFile(new URL("../.env", import.meta.url));
        } catch {
          // Ambient process.env will be used
        }
      }
    }
  }

  const config = loadConfigFromEnv();

  try {
    validateConfig(config);
  } catch (err: unknown) {
    console.error(`[Configuration Error] ${(err as Error).message}`);
    process.exit(1);
  }

  const server = createProxy(config);

  const shutdown = async (signal: string) => {
    console.log(`\nReceived ${signal}, shutting down gracefully...`);
    try {
      await server.close();
      console.log("tl-proxy closed cleanly.");
      process.exit(0);
    } catch (err) {
      console.error("Error during shutdown:", err);
      process.exit(1);
    }
  };

  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));

  try {
    const listenUrl = await server.start();
    console.log("==================================================");
    console.log("           TL Model Proxy Server                  ");
    console.log("==================================================");
    console.log(`Listening on:       ${listenUrl}`);
    console.log(`Endpoints:          POST /chatbbc/init_session`);
    console.log(`                    POST /chatbbc/chat`);
    console.log(`Upstream Provider:  ${config.upstreamProvider}`);
    console.log(`Upstream Model:     ${config.upstreamModel}`);
    console.log(`Upstream Base URL:  ${config.upstreamBaseUrl || "(none)"}`);
    console.log(`Auth Mode:          ${config.authMode}`);
    console.log(`Log Level:          ${config.logLevel}`);
    console.log("==================================================");
  } catch (err: unknown) {
    console.error("Failed to start tl-proxy server:", err);
    process.exit(1);
  }
}

main();
