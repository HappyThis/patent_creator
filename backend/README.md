# Patent Creator Backend

```bash
uv sync
uv run uvicorn app.api.app:app --reload --host 127.0.0.1 --port 8000
```

如果要启用真实 agent 写作能力，需要配置 OpenAI 兼容接口：

```bash
export OPENAI_COMPAT_BASE_URL=https://api.deepseek.com/v1
export OPENAI_COMPAT_API_KEY=your_deepseek_api_key
export OPENAI_MODEL=deepseek-v4-pro
```

## Structure

- `app/api/`: FastAPI app factory and HTTP/SSE route definitions.
- `app/services/`: Round orchestration, SSE event bus, and app service wiring.
- `app/agents/`: Agent declarations, prompts, model runtime adapters, and subagent workers.
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
- New subagent declaration, prompt, or model adapter: add it under `app/agents/`.
- New executor tool or runtime plumbing: add it under `app/runtime/`.
- New disclosure/document transformation or validation: add it under `app/domain/`.
- New persistence concern: add it under `app/storage/`.
- New request/response model: add it under `app/schemas/`.

## Near-Term Evolution

- Keep splitting `app/api/routes/` by bounded context as project, chat, export, and session concerns grow.
- Keep the main agent planner in `app/services/` light, and continue moving concrete reasoning/writing behaviors into `app/agents/`.
- Expand real-model coverage from `section_writer` to `material_analyst` and `consistency_reviewer`.
