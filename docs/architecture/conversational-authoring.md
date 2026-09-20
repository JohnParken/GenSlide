# Conversational authoring

Free exploration keeps a conversational interface while using bounded writing
operations. The planner returns `reply`, `outline`, `generate`, or `revise`.
`generate` plans the outline first, then uses the same framework-free batch writer
as the guided service (`genslide_agentscope.authoring`). Explicit user requests to
write authorize those internal steps; the guided API's confirmation checks remain
unchanged.

## Context and session ownership

- Each `ChatState.autonomous_memory` belongs to one workbench session. The agent
  copies it, then returns a replacement only after the whole turn succeeds.
- Requirement changes require verbatim evidence from the latest user message.
  Unmentioned requirements remain; explicit cancellations can remove them.
- Recent history stays verbatim within an 18,000-character budget. Older messages
  are processed into a rolling summary in bounded batches. Failed replies and
  display-only file notices do not become context. Summaries are model-produced,
  so factual retention still needs live evaluation.
- Small drafts are available in full to the planner. For long drafts, routing
  sees a labeled section preview; a discussion can request full sections using
  `section_ids`. Revision always receives the full requested sections, and code
  merges only those sections. No replacement is committed after a failed turn.
- UI aliases are saved before every rerun, including session switches. Expert
  changes preserve conversation and materials. Downloaded revisions remain in
  session memory as separate versions; browser/process persistence is unchanged.

## Materials and delivery

The UI uses the shared attachment parser, including GB18030 text and DOCX tables
in document order. Parsing errors are visible. It never silently takes a prefix.
Inputs over the shared 100,000-character limit are rejected explicitly.

Materials longer than 18,000 characters are processed in 10,000-character chunks.
Each chunk contributes validated verbatim excerpts with source character ranges;
all chunk briefs are retained. Writing additionally retrieves a relevant complete
chunk. This improves coverage but does not guarantee that summarization retains
every fact. Extraction is cached by material content hash within the session.

The writer validates structure, retries malformed JSON once, checks length,
duplicate bodies and common placeholders, and performs one corrective generation
if needed. Unresolved quality checks are shown with the draft rather than hidden.
These are structural checks, not an independent factual or professional review.

Word supports headings, lists and editable Markdown tables with basic typography.
PPT paginates excess content instead of dropping it. Templates are generic, not
certified official-document layouts. Plain writing can be downloaded as Markdown.
The UI reports real processing stages; token-by-token model streaming is not
implemented by this change.

## Model configuration and evaluation

For `MODEL_PROVIDER=tl`, the actual model is selected by the proxy's
`UPSTREAM_MODEL`; `UPSTREAM_MAX_TOKENS` and `UPSTREAM_THINKING` also belong to the
proxy. Frontend `MODEL_NAME` does not override it. For direct compatible providers,
`MODEL_NAME` selects the model and `GENSLIDE_CHAT_MAX_OUTPUT_TOKENS` controls the
output budget (default 8192). Direct responses ending with `finish_reason=length`
are rejected rather than treated as complete drafts.

Offline regressions:

```bash
.venv/bin/python -m pytest -q
PYTHONPATH=services/genslide-agentscope/src services/genslide-agentscope/.venv/bin/python -m pytest -q services/genslide-agentscope/tests
```

Inspect the synthetic evaluation cases without calling a model:

```bash
.venv/bin/python scripts/eval_chat_quality.py --dry-run
```

With the desired provider configured, run the same cases for each configuration:

```bash
.venv/bin/python scripts/eval_chat_quality.py --output /tmp/chat-quality.json
```

Live execution makes billed model calls. The report retains replies for human
review and measures requirement recall, undesired artifact creation, question
count, preservation of untouched sections, material usage, length and elapsed
time. Literal checks alone do not establish writing quality; compare actual
drafts as well. No live benchmark score is implied by passing the mocked tests.
