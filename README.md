# GenSlide — AgentScope Content Generation Workbench

GenSlide is being migrated to a Skill-driven writing assistant for editable Word/WPS documents,
PPTX decks and plain writing, through a request-bound, multi-user API backed by
**AgentScope 2.0.7.post1**.

AgentScope is the **only** engine in this repository. The earlier LangGraph service and the
original GPT-4o Streamlit pipeline have been removed; see
[Repository layout](#repository-layout) for what remains.

---

## Architecture

```
Development assistant         BFF gateway                AgentScope service            TL proxy
frontend/assistant_demo.py → mock_bff :8010         →  genslide-agentscope :8002 → tl-proxy :8089 → model
   (natural language turns)     (DEV ONLY, in memory)       (Skill → reply/outline/deliverable)
```

Each request carries its own authorization and materials; the execution service keeps no database
and no Redis. The current change separates an action's authorized execution context from its
disposable local workspace. Persistent user authoring spaces are deferred to the production BFF.
See [workspace lifetimes](docs/architecture/execution-workspaces.md) and the
[future TDSQL MariaDB 10.3 contract](docs/architecture/tdsql-mariadb-10.3-bff-contract.md).

The Streamlit workbench is a development demonstration. It is not a production
identity or persistence boundary. `frontend/assistant_client.py` defines the user-facing BFF
adapter for an existing authenticated transport; this repository contains no Vue application or
production BFF. Do not expose the dev mock to users.

---

## Repository layout

```
GenSlide/
├── frontend/
│   ├── service_chat.py          # Streamlit workbench (expert council + autonomous mode)
│   ├── assistant_demo.py        # Current server-side assistant demo (make run)
│   ├── assistant_client.py      # Public BFF adapter, supplied authenticated transport
│   ├── service_chat_client.py   # BFF client and session state
│   ├── autonomous_agent.py      # Free-form agent that drives drafting/rendering
│   └── expert_council.py        # Expert personas mapped onto skills
├── backend/                     # The content API (genslide_agentscope, tests, contracts, deploy, Dockerfile)
├── services/
│   └── tl-proxy/                # chatbbc two-stage protocol → Qwen/DeepSeek
├── scripts/                     # Local dev stack start/stop + flow check
├── tests/                       # Root workbench/agent tests
├── contracts/genslide-v1/       # Versioned request contract (schema + notes)
└── docs/                        # Architecture notes and TL protocol references
```

---

## Local development

### Prerequisites

- Python 3.12 (`uv` manages environments)
- Node.js 22 for `services/tl-proxy`

### Start the whole dev stack

```bash
make dev-agentscope      # tl-proxy :8089, mock BFF :8010, AgentScope :8002, workbench :8501
```

Then open <http://127.0.0.1:8501>. Stop everything with:

```bash
make stop-agentscope
```

The script writes logs to `.logs/` and reuses any component already listening on its port.

### Start components manually

Configure `services/tl-proxy/.env` with your `UPSTREAM_*` values first, then:

```bash
# TL proxy
cd services/tl-proxy && npm ci && npm run build && npm start

# Mock BFF
cd backend
uv run --locked uvicorn genslide_agentscope.mock_bff:create_mock_bff \
  --factory --host 127.0.0.1 --port 8010

# AgentScope service
cd backend
uv run --locked uvicorn genslide_agentscope.api:create_app \
  --factory --host 127.0.0.1 --port 8002

# Workbench (repo root)
make run
```

Shared environment for local runs:

```bash
export GENSLIDE_ENV=development
export GENSLIDE_ALLOW_MOCK=1
export GENSLIDE_SERVICE_TOKEN=local-development-token-at-least-32-characters
export GENSLIDE_BFF_URL=http://127.0.0.1:8010/internal/genslide/v1
export MODEL_PROVIDER=tl
export MODEL_BASE_URL=http://127.0.0.1:8089
export MODEL_API_KEY=local-proxy-key
export MODEL_NAME=qwen3.8-flash
```

Set `MODEL_PROVIDER=openai` to use an OpenAI-compatible provider instead of the TL proxy.

---

## Testing

```bash
make test              # root workbench/agent tests
make test-service      # backend service suite
make test-flow         # end-to-end check against the running dev stack
```

The service suite is fully self-contained and runs from its own locked environment:

```bash
cd backend && uv run --locked pytest -q
```

---

## Configuration notes

- The root environment intentionally does **not** install the `agentscope` framework. The root
  code imports only `genslide_agentscope` submodules that carry no AgentScope symbols
  (`content_io`, `skills`, `tl_provider`). Install and run the framework inside
  `backend`, which pins `agentscope==2.0.7.post1`.
- `frontend/autonomous_agent.py` adds `backend` to `sys.path`, so the
  workbench and root tests consume the service source directly rather than a second copy.
- Installing `agentscope` into the root environment is not possible alongside
  `streamlit==1.34.0`: AgentScope requires `protobuf>=5.0,<8.0` through
  `opentelemetry-exporter-otlp`, while Streamlit 1.34 pins `protobuf<5`. Raising the Streamlit
  pin would be required to merge the two environments.

---

## License

[MIT](LICENSE.md)
