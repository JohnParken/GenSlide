# tl-proxy

Independent TypeScript/Node.js testing proxy simulating the corporate `chatbbc` two-stage private protocol (`POST /chatbbc/init_session` and `POST /chatbbc/chat`) and bridging to external OpenAI-compatible models (Qwen `qwen3.8-flash` or DeepSeek).

## Features

- **Standard Two-Stage RPC Protocol**: Faithfully simulates `/chatbbc/init_session` and `/chatbbc/chat` endpoints with standard corporate success envelope (`code: 0`, `message: "success"`).
- **Native Tool Isolation Firewall**: Strictly forbids upstream and downstream native tool fields (`tools`, `tool_choice`, `functions`, `function_call`, `parallel_tool_calls`), ensuring test validation is grounded purely in prompt-defined schemas and plain content text.
- **Transparent Output Forwarding**: Raw text, Markdown, XML, JSON, or code blocks are preserved verbatim without parsing or modification.
- **Real-Time Per-Token SSE Streaming**: Forwards incremental delta tokens immediately downstream as `event: chunk` and terminates with `event: done`.
- **Four-Direction Debug Logging**: Logs complete messages with sequence and direction (`client_to_proxy`, `proxy_to_upstream`, `upstream_to_proxy`, `proxy_to_client`) and directional masking of credentials.
- **Zero Heavy Runtime Dependencies**: Built directly on native Node.js HTTP and stream primitives.

## Quick Start

### 1. Installation

```bash
cd services/tl-proxy
npm ci
npm run build
```

### 2. Configuration

Copy `.env.example` to `.env` or set environment variables:

```bash
cp .env.example .env
```

Example `.env` for Qwen (`qwen3.8-flash`):
```dotenv
TL_PROXY_PORT=8089
UPSTREAM_PROVIDER=qwen
UPSTREAM_MODEL=qwen3.8-flash
UPSTREAM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
UPSTREAM_API_KEY=sk-your-dashscope-api-key
LOG_LEVEL=debug
```

Example `.env` for DeepSeek:
```dotenv
TL_PROXY_PORT=8090
UPSTREAM_PROVIDER=deepseek
UPSTREAM_MODEL=deepseek-v4-flash
UPSTREAM_BASE_URL=https://api.deepseek.com
UPSTREAM_API_KEY=sk-your-deepseek-api-key
LOG_LEVEL=debug
```

### 3. Run Server

```bash
# Production mode (after build)
npm start

# Development mode (Node.js direct execution)
npm run dev
```

### 4. Run Tests

```bash
npm test
```

## Integration with GenSlide

In GenSlide's `.env`, configure the ChatBBC / TL provider to point at `tl-proxy`:

```dotenv
MODEL_PROVIDER=tl
MODEL_BASE_URL=http://127.0.0.1:8089
MODEL_API_KEY=local-proxy-key
MODEL_NAME=qwen3.8-flash
```

Both GenSlide services default to `MODEL_PROVIDER=tl`; setting it explicitly
is recommended for readable deployment configuration. `MODEL_PROTOCOL=tl` remains a
backward-compatible alias; if both variables are set they must have the same
value. In local proxy mode `MODEL_API_KEY` may be a non-empty development
placeholder. With `AUTH_MODE=bearer`, set it to the proxy's `TEST_ACCESS_TOKEN`.
The upstream model and public API key stay in this proxy's `UPSTREAM_*`
variables and are never sent to GenSlide.
