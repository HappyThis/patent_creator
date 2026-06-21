# Patent Creator Backend

```bash
uv sync
uv run uvicorn app.api.app:app --reload --host 127.0.0.1 --port 8000
```

To enable the real agent runtime, configure the OpenAI Responses API:

```bash
export OPENAI_API_KEY=your_openai_api_key
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-5.5
export OPENAI_REASONING_EFFORT=high
export OPENAI_MAX_OUTPUT_TOKENS=8192
```

Hosted web search is enabled by default for the main agent. Disable it only when the deployment should not call OpenAI's web search tool:

```bash
export OPENAI_WEB_SEARCH_ENABLED=false
export OPENAI_WEB_SEARCH_CONTEXT_SIZE=low
```

Only OpenAI models are supported. The backend uses the Responses API and keeps conversation history plus rolling context compression locally.

## Structure

- `app/api/`: FastAPI app factory and HTTP/SSE route definitions.
- `app/services/`: Round orchestration, SSE event bus, and app service wiring.
- `app/agents/`: Main agent prompt, model runtime adapters, and worker loop helpers.
- `app/runtime/`: Context manager plus executor engine and tool implementations.
- `app/storage/`: File-system persistence, workspace git, and export operations.
- `app/domain/`: Disclosure document structure, render transforms, and pure document tool logic.
- `app/core/`: Settings, IDs, and shared errors.
- `app/schemas/`: API DTOs.
- `tests/`: API and document-tool regression tests.

## Layering Rules

- `api -> services -> runtime/agents -> domain/storage/core/schemas`
- `services` can coordinate workflows, but should not own raw file formats, model calls, or AST transforms.
- `agents` define capability boundaries and prompt/runtime behavior, but do not own HTTP or persistence.
- `runtime` executes tools, performs permission checks, and assembles per-round context.
- `domain` stays pure and deterministic. It should be safe to unit-test without FastAPI or the filesystem.
- `storage` owns workspace layout, JSON persistence, exports, and workspace git behavior.
- `core` stays small and dependency-light.

## Where To Add Code

- New HTTP endpoint: add it under `app/api/routes/`.
- New multi-step backend workflow: add it under `app/services/`.
- New main-agent prompt behavior or model adapter: add it under `app/agents/`.
- New executor tool or runtime plumbing: add it under `app/runtime/`.
- New disclosure/document transformation or validation: add it under `app/domain/`.
- New persistence concern: add it under `app/storage/`.
- New request/response model: add it under `app/schemas/`.

## Near-Term Evolution

- Keep splitting `app/api/routes/` by bounded context as project, chat, export, and session concerns grow.
- Keep the main agent planner in `app/services/` light, and continue moving concrete reasoning/writing behaviors into `app/agents/`.
- Keep real-model coverage focused on the main agent's document reading, planning, editing, and recovery behavior.
