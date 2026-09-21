# Cairn — Agent Guide

## Project overview

Cairn is a general-purpose problem-solving engine built on a **Blackboard Architecture**: given an `origin` and a `goal`, concurrent agent workers search a path through an unknown state space. Its first validated domain is AI penetration testing / CTF. It defines no agent roles and no workflows; tasks are generated at runtime from the current state of a shared fact-intent graph.

Three graph primitives (the full protocol is specified in `docs/specs/server-protocol.md`, in Chinese):

- **Fact** — a confirmed, objective finding written to the board (append-only; state changes are expressed by appending new facts).
- **Intent** — a declared direction of exploration (graph edge, possibly a hyper-edge with multiple `from` facts). Lifecycle: unclaimed (`worker=null`) → claimed (heartbeat) → concluded (produces one new Fact).
- **Hint** — human judgment injected at any time; not part of the causal graph.

Runtime architecture (four components, spec in `docs/specs/dispatcher-design.md`):

1. **Cairn Server** (`cairn serve`) — FastAPI + SQLite protocol truth source. Maintains graph consistency only; does no reasoning. Serves a web UI (single `index.html` with vendored Cytoscape/Alpine/Tailwind) on port 8000 and persists to `~/.local/share/cairn/cairn.db`.
2. **Dispatcher** (`cairn dispatch --config dispatch.yaml`) — the client executor and **sole protocol writer**. Reads the graph, schedules tasks, manages per-project workspaces and worker processes, writes results back. Agents never call the Cairn API directly; they only receive a rendered prompt and return structured JSON on stdout.
3. **Project workspaces** — one working directory per project on the dispatcher host (`<workspace_root>/<project_id>/`), managed by the local backend; `local.completed_action` (`keep`/`remove`) controls whether it is deleted after completion.
4. **Workers / Agent CLIs** — pluggable LLM CLI backends: `claudecode` (Claude Code), `codex`, `pi`, and `mock` (deterministic simulator for testing).

Four task types, all executed by the same worker:

| Task | Trigger | Output |
|------|---------|--------|
| `bootstrap` | Project in initial state (only origin/goal facts) | `fact + complete` (main phase) or `fact` (conclude fallback) |
| `reason` | New facts/hints arrived; claims the per-project `reason` lease | `complete` / new `intent`s / no-op |
| `explore` | An unclaimed open intent exists | One fact conclusion |
| `writeup` | Project completed and no writeup stored | Writeup markdown stored via `PUT /projects/{id}/writeup` |

`bootstrap` and `explore` support a two-phase mode: on timeout or unparseable output, the same session is resumed with a `*_conclude` prompt that only summarizes confirmed findings. `writeup` is single-phase with no lease: it builds a reproducible Chinese writeup from the successful fact chain plus execution records extracted from claude session transcripts, and retries 30s after failure.

**Execution**: local only. Workers run as host subprocesses reusing the machine's logged-in `claude`/`codex`/`pi` CLIs — no Docker, no API keys in the config. On startup the dispatcher checks each worker CLI is on `PATH` and runnable (`--startup-healthcheck-only` runs only this check). Run the dispatcher directly on the host.

## Repository layout

```
cairn/                    # The Python package (uv project, src layout)
  pyproject.toml          # Package metadata, deps, pytest config
  uv.lock                 # Locked dependencies
  src/cairn/
    cli.py                # `cairn` CLI entry (click): serve | dispatch
    server/               # FastAPI app
      app.py  db.py  models.py  services.py  report_transcripts.py
      routers/            # settings, llm, projects, hints, intents, export, writeups
      static/             # Web UI (index.html + vendored JS)
    dispatcher/
      config.py           # Pydantic models + validation for dispatch.yaml
      contracts.py  models.py  output_parser.py  prompting.py  logging.py
      prompts/{default,mock}/   # Markdown prompt templates (shipped with code; includes writeup.md)
      protocol/client.py  # HTTP client for the Cairn server API
      scheduler/          # loop.py (main loop), worker_select.py
      tasks/              # bootstrap.py, reason.py, explore.py, writeup.py, common.py
      workers/            # base.py, registry.py, adapters/{claudecode,codex,pi,mock}.py
      runtime/            # backend, local_backend, local_process,
                          # process, heartbeat, cancellation
  tests/                  # pytest suite (see Testing)
docs/specs/               # Protocol & dispatcher design specs (Chinese, authoritative)
cairnctl.sh               # Host-side service manager: start|stop|restart|status|logs (comments in Chinese)
dispatch.local.example.yaml # Local-mode config template
dispatch.mock.yaml        # Mock-driver demo config (no LLM needed)
dispatch.yaml             # Your local config (gitignored, create from an example)
datas/                    # Runtime data: datas/run (pid/log), datas/local (workspaces)
```

## Technology stack and build

- Python ≥ 3.12, managed with **uv** (`uv_build` backend). Runtime deps: FastAPI, uvicorn, click, PyYAML, requests. Models use Pydantic v2.
- The default uv index is an Aliyun mirror (`[[tool.uv.index]]` in `cairn/pyproject.toml`); use `-i` to override if needed.
- No linter/formatter is configured — match surrounding code style (type hints, `from __future__ import annotations`, stdlib + listed deps only).
- Storage is plain SQLite; the schema lives in `cairn/server/db.py` and migrations are tested by `tests/test_db_migrations.py`.

## Build and run commands

```bash
# Install / sync (from repo root)
uv sync --project cairn

# Server (default 127.0.0.1:8000; SQLite at ~/.local/share/cairn/cairn.db)
uv run --project cairn cairn serve

# Dispatcher
cp dispatch.local.example.yaml dispatch.yaml
uv run --project cairn cairn dispatch --config dispatch.yaml
uv run --project cairn cairn dispatch --config dispatch.yaml --startup-healthcheck-only  # local CLI checks only
uv run --project cairn cairn dispatch --config dispatch.yaml --once                      # single scheduling tick

# Host-side manager (writes pid/log files under datas/run/)
./cairnctl.sh {start|stop|restart|status|logs}
```

## Testing

```bash
uv run --project cairn --group dev pytest
```

- pytest with `testpaths = ["tests"]`; dev group adds `pytest` and `httpx` (FastAPI `TestClient`).
- The suite is fast and hermetic: no network, no live LLM endpoints. The `mock` worker driver simulates every outcome (success, rejection, invalid JSON, command failure, timeout) via `MOCK_<PHASE>` JSON env vars with probability distributions.
- `tests/conftest.py` provides fakes (`FakeClient`, `FakeDriver`, `FakeBackend`, `make_config`, `make_project`) — reuse them for new dispatcher tests instead of hitting a real server.
- Coverage spans: server API (`test_server_api.py`), DB migrations, scheduler logic, worker tasks, contracts/drivers, config/adapters, local execution, runtime logic, startup CLI checks, and a mock-driver end-to-end run (`test_mock_end_to_end.py`).
- To exercise the full pipeline manually without LLMs, use `dispatch.mock.yaml` (local + mock drivers) against a running server.

## Configuration conventions

- `dispatch.yaml` is the only runtime config; prompts ship as markdown under `cairn/src/cairn/dispatcher/prompts/<group>/` selected by `runtime.prompt_group` (`default` or `mock`). Prompt placeholders (`{graph_yaml}`, `{fact_ids}`, `{intent_id}`, `{origin}`, etc.) are validated at load time.
- Env precedence: `common_env` < per-worker `env`.
- Each worker models one independent LLM concurrency quota unit (don't split one account/quota across workers); concurrency is controlled by `workers[].max_running`, plus `runtime.max_workers`, `runtime.max_running_projects`, `runtime.max_project_workers`.
- `runtime.interval` is deliberately reused as both the scheduler loop tick and the heartbeat period for claimed tasks — do not decouple without reading `docs/specs/dispatcher-design.md`.
- `active_worker` in `dispatch.yaml` hot-reloads (mtime-based) to switch the active worker without restart.
- Full field reference: the "配置字段速查" section of `docs/specs/dispatcher-design.md`.

## Code conventions and design rules

- Documentation language is mixed: README, code, and prompts are English; the design specs (`docs/specs/*.md`) and `cairnctl.sh` comments are Chinese. Follow the language of the file you are editing.
- **The Dispatcher is the only protocol writer.** Agents receive a prompt and return one raw JSON object on stdout (`{"accepted": true, "data": {...}}`); never make worker code call the Cairn API directly.
- Facts are append-only. Facts, intents, and complete/claim/release semantics must match `docs/specs/server-protocol.md` exactly (atomic conclude, 403 for non-active projects, 409 for lease conflicts, `stopped` as a hard stop that clears claims).
- The dispatcher is designed and tested as a **single instance** per server; multi-dispatcher coordination is out of scope.
- Failed writes/parses are logged and dropped — no immediate retries; a worker that returns `accepted: false` gets a short `retry_after` window before it is selected again.
- Logging philosophy: steady-state polling/heartbeats stay quiet; state changes (workspace creation, task dispatch, timeouts, releases) must be visible.
- When changing anything documented in `docs/specs/`, update those files to match.

## Security considerations

- Cairn is an offensive-security tool. Use it only in environments where you have explicit authorization (see the Disclaimer in `README.md`). Prompts frame the context as an authorized pentest competition/range.
- Workers run agent CLIs with permission bypasses (`--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`) **directly on the host, with your user's permissions and no sandbox** — treat accordingly. Only point Cairn at environments you are authorized to operate in.
- Each worker inherits the dispatcher's host environment (plus `common_env` / per-worker `env`), including the CLIs' own logged-in credentials; never commit real endpoints or tokens, and keep example configs placeholder-only.
