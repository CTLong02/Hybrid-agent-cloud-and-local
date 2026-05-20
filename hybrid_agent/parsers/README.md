# Hybrid Coding Agent

Production-ready hybrid: **Claude (planner + reviewer)** + **local Qwen3-Coder 30B (worker)**.

## Architecture

```
                ┌─────────────────────────────────────────┐
                │  Task list (.md / .xlsx / .docx /       │
                │             .yaml / .json / .csv)       │
                └────────────────────┬────────────────────┘
                                     │ parse
                                     ▼
                          ┌──────────────────────┐
                          │   DAG (deps, ready)  │
                          └──────────┬───────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
       ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
       │   Planner   │  ───▶  │   Router    │  ───▶  │   Reviewer  │
       │  (Claude)   │        │ (rule chain)│        │  (Claude)   │
       └─────────────┘        └──────┬──────┘        └─────────────┘
                                     │
                            ┌────────┴────────┐
                            ▼                 ▼
                     ┌────────────┐    ┌────────────┐
                     │ LocalWorker│    │ClaudeWorker│
                     │ (Ollama)   │    │  (SDK)     │
                     └────────────┘    └────────────┘
                            │                 │
                            └────────┬────────┘
                                     ▼
                            ┌────────────────┐
                            │ Sandbox commit │
                            │  (git worktree)│
                            └────────────────┘
```

Each task flows: **Plan (Claude) → Route → Code (Local or Claude) → Review (Claude, may auto-fix) → Commit**. Every state transition is persisted to SQLite, so a kill -9 mid-run still resumes cleanly.

## Why this architecture

- **Claude does what it's best at**: reading the codebase, breaking tasks into concrete plans, and reviewing diffs critically. These are token-light operations.
- **Local does what it's good enough at**: following a concrete plan and emitting code. The token-heavy operation lives where it's free.
- **Routing is pluggable**: hard rules (security tags), failure escalation, complexity-based, path-based. First-match-wins. Add custom rules in `routing.py`.
- **Sandbox-per-task**: git worktrees give each task its own branch. No cross-task interference, easy human review.

## Install

```bash
# 1. Install Ollama and pull the model
#    (Qwen3-Coder 30B Q4 fits in ~11GB VRAM)
ollama pull qwen3-coder:30b

# 2. Install Claude Code CLI and log in (no API key needed)
#    See https://docs.claude.com/claude-code for installer
claude   # will open OAuth in browser

# 3. Install this package
cd hybrid_agent
pip install -e .
```

## Configure

```bash
cp config.example.yaml config.yaml
# Edit project_root and any tuning knobs
```

## Run

```bash
# Run all tasks from a file
hybrid-agent run -c config.yaml -t tasks.example.md

# Check status
hybrid-agent status -c config.yaml

# Inspect one task in detail (full plan / code / review JSON)
hybrid-agent show -c config.yaml T003

# Reset failed/blocked tasks for a retry, then re-run
hybrid-agent reset -c config.yaml
hybrid-agent run -c config.yaml -t tasks.example.md
```

## Task schema

The agent auto-defines a simple schema. All formats use the same fields:

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Stable unique identifier (e.g. `T001`) |
| `title` | yes | Short title |
| `description` | no | Free text |
| `depends_on` | no | List of task ids; this task waits for all to be DONE |
| `tags` | no | Used by routing (e.g. `security` → forces Claude) |
| `complexity` | no | `low` / `medium` / `high` (default `medium`) |
| `target_files` | no | Hint for the planner & path-glob routing |
| `acceptance_criteria` | no | What "done" means; reviewer checks against this |

### Markdown

```markdown
# Task: T001 — Create Student API
- depends_on: T000
- tags: api, crud
- complexity: low
- files: src/api/student.py
- acceptance: POST /students returns 201

Description body (multi-line OK).
```

### YAML / JSON

```yaml
tasks:
  - id: T001
    title: Create Student API
    depends_on: [T000]
    tags: [api, crud]
    complexity: low
    files: [src/api/student.py]
    acceptance: POST /students returns 201
    description: |
      Multi-line description.
```

### Excel / CSV

Header row with columns: `id, title, description, depends_on, tags, complexity, target_files, acceptance_criteria`. List fields are semicolon- or comma-separated.

### Word (.docx)

Same shape as markdown; `Heading 1` paragraphs delimit tasks, plain paragraphs become field lines / description.

## Resume behavior

The orchestrator treats SQLite as the source of truth.

- On startup, any task left in a transient status (`PLANNING`, `CODING`, `REVIEWING`, `FIXING`) is **rolled back** to its previous completed-stage status. Already-committed work in the sandbox is preserved.
- Tasks in `DONE` / `FAILED` / `BLOCKED` are not retried automatically. Use `hybrid-agent reset` to clear failed/blocked back to `READY`.
- The sandbox directory is reused across runs — if the worktree exists, we skip recreation. Delete `.hybrid_agent_sandboxes/<task_id>` to force a fresh one.

## Routing rules (production)

Defined in `routing.py`, evaluated in order:

1. **AvailabilityRule** — if local is unhealthy, route to Claude (and vice versa)
2. **ForceTagRule** — `force_claude_tags` or `force_local_tags` from config
3. **FailureEscalationRule** — after N local failures, escalate to Claude
4. **PathGlobRule** — glob match on `target_files` → Claude
5. **ComplexityRule** — `complexity: high` → Claude
6. **Default** — falls through to `default_backend`

To add a custom rule, implement the `RoutingRule` protocol and insert it in `build_default_router()`.

## Concurrency model

With **12GB VRAM** you can run **one** Qwen3-Coder 30B endpoint. The orchestrator therefore enforces:

- Local pool concurrency = number of endpoints in `local.endpoints` (default 1)
- Claude pool concurrency = `claude.concurrency` (default 1)
- Task-level concurrency = `orchestrator.max_concurrent_tasks` (default 4)

While the local LLM serves task A, task B can be reading files / running tests / waiting on Claude — that's where the parallelism comes from. To get true LLM-level parallelism, add more endpoints to the `local.endpoints` list (another machine running Ollama, or a vLLM cluster).

## Failure handling

- **Per-call retry**: each LLM call retries up to `retry.max_attempts` with exponential backoff. Auth/4xx errors are classified as fatal and don't retry.
- **Per-task timeout**: `plan_timeout_seconds`, `code_timeout_seconds`, `review_timeout_seconds`.
- **Review iterations**: if reviewer says `needs_fix`, the worker re-runs with the issue list appended to the plan, up to `review_max_iterations`.
- **Graceful shutdown**: SIGINT/SIGTERM cancels in-flight tasks, persists state, and exits.
- **Crash recovery**: on next run, in-progress tasks are rolled back to a safe checkpoint (see Resume above).

## File map

```
hybrid_agent/
├── pyproject.toml
├── config.example.yaml
├── tasks.example.md
├── tasks.example.yaml
├── README.md
└── hybrid_agent/
    ├── __main__.py        # python -m hybrid_agent
    ├── cli.py             # click commands
    ├── config.py          # Pydantic config models
    ├── models.py          # TaskSpec, TaskExecution, enums, outputs
    ├── state.py           # SQLite store + resume logic
    ├── dag.py             # dependency graph
    ├── retry.py           # retry policy + error classification
    ├── sandbox.py         # git worktree manager
    ├── routing.py         # pluggable routing rules
    ├── orchestrator.py    # main async loop
    ├── parsers/
    │   ├── base.py        # registry + shared helpers (build_spec, split_list)
    │   ├── markdown.py    # .md  / .markdown  (also reused by word.py)
    │   ├── yaml_json.py   # .yaml / .yml / .json
    │   ├── csv.py         # .csv
    │   ├── excel.py       # .xlsx / .xlsm
    │   └── word.py        # .docx
    ├── llm/
    │   ├── base.py        # LLMClient + LLMPool abstraction
    │   ├── ollama.py      # local backend
    │   └── claude.py      # claude-agent-sdk wrapper
    └── agents/
        ├── planner.py     # Claude, read-only, JSON output
        ├── worker.py      # LocalWorker (JSON) + ClaudeWorker (SDK tools)
        └── reviewer.py    # Claude with auto-fix authority
```

## Limitations to be honest about

- **30B local model has a real failure rate** on agentic coding (≥30% rework expected on a mature codebase). The reviewer's auto-fix authority and `escalate_to_claude_after_failures` exist to compensate, but expect to babysit early on.
- **A 12GB VRAM single-endpoint setup means LLM calls are serial.** Adding more endpoints (more machines / cloud GPUs) is the way to scale.
- **Resume is best-effort.** If the process crashed mid-write to the sandbox filesystem, you may need to manually inspect the worktree.

## License

MIT.
