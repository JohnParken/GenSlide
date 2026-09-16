# TL wire contract

This is the repository compatibility baseline plus the selected test-simulator behavior, not a complete corporate gateway specification. Implement the simulator in TypeScript on Node.js. Examples use synthetic values.

## Shared request envelope

Both endpoints receive JSON with appId: string, trCode: string, trVersion: string, timestamp: number (Unix milliseconds), requestId: string, and endpoint-specific data.

The client sends all metadata fields, allowing empty appId/trCode/trVersion. The development proxy does not enforce them. Preserve these fields; do not make them new mandatory non-empty credentials. Authentication is independent. The default local test mode binds to loopback and needs no organization identity integration. A shared test environment may enable a separate Bearer credential; examples below use the default local mode. appId is metadata, not a credential. In the new package reject supplied metadata of incorrect types, but retain omitted metadata in the compatibility profile. Generate an internal correlation ID if needed.

## POST /chatbbc/init_session

Request headers: Content-Type: application/json.

```json
{
  "appId": "internal-demo",
  "trCode": "agent-chat",
  "trVersion": "1.0",
  "timestamp": 1789258985000,
  "requestId": "init-example-1",
  "data": {
    "prompt_variables": [
      {"name": "system_prompt", "value": "Reply in the text format requested by the user."}
    ]
  }
}
```

HTTP 200; JSON body:

```json
{"code":0,"message":"success","data":{"session_id":"session_example"}}
```

Observed rules:

- Missing prompt_variables means an empty array; empty arrays and name-only legacy arrays are accepted.
- Every item has a nonblank string name and a string value. Reject duplicate exact names. Do not silently trim or rename variables.
- Configured system variable defaults to system_prompt; its configuration must be trimmed, nonblank, and different from reserved name.
- If any variable other than name is present, the configured system variable must exist and contain nonblank text. Other variables may coexist and are stored, but not implicitly interpolated.
- init allocates session state only; it does not call a model.
- name is not a Qwen routing override. The unified facade also keeps routing server-controlled for DeepSeek.

## POST /chatbbc/chat

Request headers: Content-Type: application/json; the client also sends Accept: text/event-stream.

```json
{
  "appId": "internal-demo",
  "trCode": "agent-chat",
  "trVersion": "1.0",
  "timestamp": 1789258985100,
  "requestId": "chat-example-1",
  "data": {
    "session_id": "session_example",
    "txt": "Reply with a short Markdown heading and a one-line answer.",
    "files": [{"file_id":"","url":"","content_type":""}],
    "stream": true
  }
}
```

- session_id must be a nonblank string identifying an existing session; txt must be a string (empty is currently allowed).
- stream is a boolean, default true when absent. Reject string "false", numeric values and null. The body flag controls output, not Accept alone.
- Accept missing files, files: [], and the empty placeholder above. Current proxies ignore real files. Proposed internal v1 rejects non-empty file references with 400; this is a documented tightening, not existing attachment support.
- With a bound system variable, construct exactly two messages: system with the bound value, user with txt. Preserve both strings, including whitespace and embedded role markers.
- Without a system variable, the compatibility profile permits legacy parsing: split line-start system:, user:, assistant: markers, trim each role segment; without markers send txt as one user message. Isolate this path; deployment policy can disable it. Native-variable requests never use it.
- Do not append prior chats or delete the session after one chat. New-service expiration is explicit; unknown/expired sessions return 404.

Non-streaming success (stream:false), HTTP 200:

```json
{"code":0,"message":"success","data":{"txt":"# Answer\nHello."}}
```

data.txt is an opaque string: plain text, Markdown, XML, JSON, code, empty or whitespace text are all permitted. The outer response must still be valid JSON; this does not impose JSON syntax on the inner model output. Do not parse, repair or strip the inner string. No upstream choices/model/usage/tool_calls envelope replaces it.

Streaming success (stream:true), HTTP 200, Content-Type: text/event-stream; charset=utf-8, Cache-Control: no-cache, no-transform, X-Accel-Buffering: no:

```text
id: example-0
event: chunk
data: {"content":"# Answer\nHello."}

id: example-1
event: done
data: {"finished":true}

```

Every frame ends with a blank line. IDs and chunk boundaries are not stable values; current code uses 32 Unicode code points per content chunk. Concatenated content must equal upstream content exactly. Do not use plain OpenAI data-only frames: the TL parser ignores unnamed message events. [DONE] is accepted by the client but is not the proxy's current emitted format.

The new package changes delivery timing: stream:true (including the default) requires upstream stream:true. Forward each complete upstream SSE event's non-empty delta.content immediately as a TL chunk. Do not wait for final JSON, buffer the full response, or preserve the old 32-character batching. A delta may contain multiple tokens; TCP boundaries are not token boundaries. Keep stream:false as a complete JSON response. Streaming success requires the configured provider's verified terminal sequence; the default requires finish_reason:stop followed by [DONE], then exactly one TL done. EOF alone is failure. See implementation-plan.md for the parser state machine.

## Prompt-defined output and unavailable native tooling

The organization has not exposed native tool_calls fields. This simulator therefore sends no tools/tool_choice/functions/function_call/parallel_tool_calls, and no response_format. A prompt asking for JSON or describing tools is forwarded unchanged; it does not enable native API features.

Reject unsupported native tool-control keys (tools, tool_choice, functions, function_call, parallel_tool_calls, tool_calls) at the TL root/data level with 400 rather than forwarding them. The same words inside prompt-variable values or txt are ordinary text and must pass unchanged.

Tool-like JSON/XML/text returned within content is ordinary output. Never execute it, convert it into native tools, or validate a tool schema. Unexpected non-empty native tool_calls/function_call in upstream message/delta is a protocol mismatch, including when content is also present: 502 before downstream headers, otherwise an error event and close. Null/empty-array placeholders are allowed. Remove the old proxy's conversion to tool_name/parameters.

Only validate the API envelope and SSE data JSON, not model-body syntax. A normally terminated empty response is permitted. Default debug logs capture complete bidirectional messages and SSE events, with credentials masked, as specified in implementation-plan.md.

## Errors and proposed normalization

Observed request failures use a non-2xx status and {"error":"..."}; Qwen upstream failures often become HTTP 500. Errors do not currently use {code,message,data}.

The default error body remains exactly {"error":"safe explanation"}. Keep correlation IDs in logs or an X-Request-Id response header; do not add message/requestId body fields by default. The current client's generic fallback on an error-only body is accepted. Any enhanced error body needs a separately selected extension. Proposed status policy (a documented change from the development proxy):

| Condition | HTTP |
| --- | --- |
| Invalid input or unsupported real attachments | 400 |
| Caller authentication/authorization | 401/403 |
| Unknown, expired or foreign session | 404 |
| Wrong method, with Allow header | 405 |
| Body too large | 413 |
| Caller rate limit | 429 |
| Invalid provider response, network or provider credential failure | 502 |
| Provider overload/429 or session capacity | 503 |
| Provider deadline | 504 |
| Unexpected internal failure | 500 |

These mappings are new operational policy; compare with company fixtures before deployment. Do not leak raw provider error bodies or keys.

After SSE headers, send one event: error with JSON {"message":"safe explanation"} and close; never follow with done or an HTTP JSON body. On disconnect abort upstream and release resources. Validate upstream status and SSE content type before sending downstream headers: such failures can still return non-2xx JSON. Once headers have been flushed, parse errors, provider errors, premature EOF, truncation, timeouts and size-limit failures are SSE errors, even after some content has already reached the client. Any text fragment, including incomplete JSON/XML or Markdown, is valid model content; its syntax is not a proxy error. Never emit success done after a stream failure.
