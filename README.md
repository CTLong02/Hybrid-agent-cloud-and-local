# Hybrid Coding Agent

Hybrid: **Claude (planner + reviewer)** + **local Qwen3-Coder 30B (worker)**.

Plan with the smartest model, code with the cheapest one, review with the smartest one again. Every state transition is persisted; resume after crashes is built-in. Cost tracking, budget caps, structured logging, and a pytest gate before any task is marked DONE.

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
       └─────────────┘        └──────┬──────┘        └──────┬──────┘
                                     │                      │
                            ┌────────┴────────┐             ▼
                            ▼                 ▼      ┌────────────┐
                     ┌────────────┐    ┌────────────┐│  pytest    │
                     │ LocalWorker│    │ClaudeWorker││ (test gate)│
                     │ (Ollama)   │    │  (SDK)     │└──────┬─────┘
                     └────────────┘    └────────────┘       │
                            │                 │             ▼
                            └────────┬────────┘      ┌─────────────┐
                                     ▼               │ Verdict +   │
                            ┌────────────────┐       │ test result │
                            │ Sandbox commit │       └──────┬──────┘
                            │  (git worktree)│              │
                            └────────────────┘              ▼
                                                     ┌─────────────┐
                                                     │  DONE / fix │
                                                     └─────────────┘

  Cross-cutting:  CostMeter (token + USD), structured logs (trace_id,
                  task_id, stage), SQLite state, retry policy.
```

Each task flows: **Plan (Claude) → Route → Code (Local or Claude) → Review (Claude, may auto-fix) → Test gate (pytest) → Commit**. Anything but a passing test downgrades the verdict to `needs_fix` and re-runs the worker.

## Why this architecture

- **Claude does what it's best at**: reading the codebase, breaking tasks into concrete plans, and reviewing diffs critically. Token-light operations.
- **Local does what it's good enough at**: following a concrete plan and emitting code. Token-heavy operation lives where it's free.
- **Routing is pluggable**: hard rules (security tags), failure escalation, complexity-based, path-based. First-match-wins. Add custom rules in `routing.py`.
- **Sandbox-per-task**: git worktrees give each task its own branch (or a plain copy if not a git repo). No cross-task interference.
- **Test gate before DONE**: an LLM-approved task whose tests fail is still `needs_fix`. Auto-detects the test runner from project marker files (Python, Node, Go, Rust, Java, Ruby, PHP, Elixir).
- **Cost guard built-in**: per-task and per-run USD caps with a hard-stop or warn mode. Prevents a runaway loop from burning $100 on a single task.

## Install

```bash
# 1. Install Ollama and pull the model (Qwen3-Coder 30B Q4 fits in ~11 GB VRAM)
ollama pull qwen3-coder:30b

# 2. Install Claude Code CLI and log in (no API key needed)
claude   # opens OAuth in browser

# 3. Install this package
cd hybrid_agent
pip install -e .

# Optional: dev tools (ruff, mypy, pytest, pytest-cov, respx)
pip install -e ".[dev]"

# Optional: Streamlit web UI (dashboard / tasks / run / config / cost & logs)
pip install -e ".[webui]"
```

## Configure

```bash
cp config.example.yaml config.yaml
# Edit project_root, cost caps, log paths, model, etc.
```

Key config sections:

```yaml
project_root: ./workspace
log_level: INFO
log_file: hybrid_agent.log          # plain text
log_json_file: hybrid_agent.jsonl   # one JSON object per line, optional

cost:
  per_task_usd_cap: 5.0     # null = no cap
  per_run_usd_cap: 50.0
  on_exceed: stop           # stop | warn

orchestrator:
  max_concurrent_tasks: 4
  run_tests_in_review: true     # block DONE on failing tests
  require_tests: false           # task without tests still passes
  test_timeout_seconds: 300
  test_command: null             # null = auto-detect from project markers

  # Per-stage timeouts are scaled by complexity at run time. Defaults below
  # multiply the base timeouts by 0.75 / 1.0 / 2.0, so a high-complexity task
  # gets twice the budget without dragging up the budget of every small task.
  plan_timeout_seconds: 600
  code_timeout_seconds: 1800
  review_timeout_seconds: 600
  complexity_timeout_multiplier:
    low: 0.75
    medium: 1.0
    high: 2.0

  # System / integration test for `merge --review`. null = auto-detect a test
  # location (tests/integration, tests/e2e, tests/system, integration_tests,
  # or e2e/ with package.json scripts). Set explicitly for custom flows like
  # "docker compose up -d && pytest tests/system && docker compose down".
  system_test_command: null
  system_test_timeout_seconds: 900
```

## Run

```bash
# Run all tasks from a file
hybrid-agent run -c config.yaml -t tasks.example.md

# Same, but auto-recover after transient failures: if any task ends FAILED or
# BLOCKED, reset them and re-run, up to N extra rounds. Stops on no progress.
hybrid-agent run -c config.yaml -t tasks.example.md --auto-resume 5

# Status table (Status / Backend / Attempts / Reviews / Last error)
hybrid-agent status -c config.yaml

# Inspect one task in detail (full plan / code / review JSON)
hybrid-agent show -c config.yaml T003

# Reset failed/blocked tasks back to READY for retry
hybrid-agent reset -c config.yaml
hybrid-agent reset -c config.yaml --task-id T004    # one specific

# Merge all DONE-task sandboxes into one output directory
# (later tasks in topo order win on file conflicts)
hybrid-agent merge -c config.yaml -t tasks.md -o merged
hybrid-agent merge -c config.yaml -t tasks.md --dry-run  # preview only

# Merge with a post-merge review gate. Runs four passes on the merged tree:
#   D - deterministic reconcile across tasks (requirements*.txt / .env* /
#       __init__.py union; pyproject.toml dep-conflict warn-only)
#   A - content-drift warnings + py_compile every .py
#   C - project unit tests (auto-detected or via test_command)
#   F - integration / e2e / system test (auto-detected from
#       tests/integration|tests/e2e|tests/system|integration_tests|e2e/
#       or set explicitly via system_test_command)
# Exits non-zero on any hard failure.
hybrid-agent merge -c config.yaml -t tasks.md --review -o merged

# Add an LLM (Claude) semantic review on top of --review. Catches stale
# references, missing wiring, and cross-task drift the deterministic gates
# can't see. Costs ~one Claude call. Verdict FAIL is a hard fail; WARN is
# printed but non-blocking. Default mode 'inline' embeds conflict files
# into the prompt (no tool use, dodges the Windows Claude-CLI flake);
# pass --llm-review-mode tools to let Claude Read/Grep/Glob the tree.
hybrid-agent merge -c config.yaml -t tasks.md --review --llm-review -o merged

# Same, plus auto-fix on FAIL: if the LLM review verdict is FAIL, Claude
# is given Edit/Write access on the merged tree to fix the flagged issues
# in place, then the review is re-run (inline) to verify. Hard fail only
# if the post-fix review still flags FAIL. Implies --llm-review.
hybrid-agent merge -c config.yaml -t tasks.md --review --llm-fix -o merged

# Tear down per-task worktrees and `agent/<task_id>` branches after a
# successful merge. Only touches tasks whose files contributed to the
# merge; failed/skipped sandboxes stay for debugging. Composes with
# --review / --llm-fix (cleanup runs after the gate passes). No-op with
# --dry-run.
hybrid-agent merge -c config.yaml -t tasks.md -o merged --cleanup
```

## Web UI

A Streamlit dashboard exposes the same workflow as the CLI: edit tasks, kick off a run, follow live status, tweak `config.yaml`, and watch cost / log tails. Requires the `webui` extra (see [Install](#install)).

```bash
# Launch with defaults: localhost:8501, opens a browser tab
hybrid-agent webui -c config.yaml -t tasks.md

# Bind to all interfaces (e.g. for a remote dev box) and pick a custom port
hybrid-agent webui -c config.yaml -t tasks.md --host 0.0.0.0 --port 8600

# Headless — don't auto-open the browser
hybrid-agent webui -c config.yaml -t tasks.md --no-browser
```

`--tasks` doesn't need to exist yet — you can create / import one from the **Tasks** page.

Pages in the sidebar:

- **Dashboard** — config/tasks/state health line and a status-count overview.
- **Tasks** — view, edit, add, or import a `tasks.md` (Markdown / YAML / JSON / Excel / CSV / Word).
- **Run** — start / stop the pipeline (incl. `--auto-resume`) and tail live progress.
- **Config** — edit `config.yaml` (models, routing, cost caps, timeouts) in-place.
- **Cost & Logs** — token spend per task / model plus a live log tail.

You can also bypass the CLI and launch Streamlit directly — useful if you want to attach a debugger or use a custom Streamlit config:

```bash
HYBRID_AGENT_WEBUI_CONFIG=config.yaml \
HYBRID_AGENT_WEBUI_TASKS=tasks.md \
streamlit run hybrid_agent/webui/app.py
```

## Generating tasks.md from a request or SRS

The `generate-tasks` command turns a natural-language request — or an SRS document plus a feature scope — into a valid `tasks.md`. It uses Claude (via the same SDK as the planner/reviewer) and grounds the decomposition in your codebase when `project_root` is set.

For follow-up requirements after the first run, see [Incremental workflow](#incremental-workflow) below — `--append` continues the id series and `--update <TASK_ID>` produces migration tasks that edit an old task's output instead of restarting from scratch.

```bash
# From a one-line request
hybrid-agent generate-tasks -c config.yaml \
  -f "Build a CRUD API for managing students, with auth and tests" \
  -o tasks.md

# From a longer request stored in a file (note the @ prefix)
hybrid-agent generate-tasks -c config.yaml \
  -f @feature_request.txt \
  -o tasks.md

# Grounded in an SRS document
hybrid-agent generate-tasks -c config.yaml \
  --srs docs/SRS.md \
  -f "Implement section 4.3 — Order placement workflow" \
  -o tasks.md

# Just print the skill prompt (no LLM call, no config needed)
hybrid-agent generate-tasks --print-prompt > task_generator.md
```

### Picking the model

By default `generate-tasks` doesn't pass a model to the SDK, so it inherits whatever the local `claude` CLI is currently set to. To pin a specific model for task generation only — independent of `planner_model` / `reviewer_model` / `coder_model` — set `claude.task_generator_model`:

```yaml
claude:
  enabled: true
  planner_model: claude-sonnet-4-5
  reviewer_model: claude-sonnet-4-5
  coder_model: claude-sonnet-4-5
  task_generator_model: claude-opus-4-7   # used only by `generate-tasks`
```

Leave the key out (or set it to `null`) to keep the SDK default. The same field is exposed in the TUI (`hybrid-agent tui`) and the Web UI's **Config** page.

Supported SRS formats: `.md`, `.markdown`, `.txt`, `.rst`, `.yaml`, `.yml`, `.json`, `.docx`. For PDFs, convert first (e.g. `pdftotext srs.pdf srs.txt`).

The skill prompt itself lives at `hybrid_agent/prompts/task_generator.md`. It's a standalone prompt — you can paste it into Claude.ai or any other LLM directly. The CLI just wraps invocation, codebase grounding, and post-validation.

After generation, review `tasks.md` and edit by hand before running. The decomposition is a starting point, not a final answer.

## Incremental workflow

Once a project has been kicked off and tasks have been run at least once, two flags on `generate-tasks` cover the common follow-up scenarios. Both keep the existing `tasks.md`, append new sections, and continue the id series so you don't need to renumber by hand.

### New requirements that don't touch existing work — `--append`

```bash
hybrid-agent generate-tasks -c config.yaml \
  -f "Add JWT authentication and a /me endpoint" \
  --append

hybrid-agent run -c config.yaml -t tasks.md --auto-resume 5
```

What happens:

- The existing `tasks.md` is parsed; the next id is computed from the highest existing id (e.g. `T021` → new tasks start at `T022`).
- Claude is told what's already covered (so it doesn't duplicate work) and which id to start from.
- New sections are appended to `tasks.md` after a `<!-- appended via generate-tasks --append -->` marker.
- On the next `run`, old DONE tasks are skipped automatically; only the new ones execute.

### Updating an existing task — `--update <TASK_ID>` (migration mode)

When the new requirement is *changing* something an old task already built, don't reset the old task — generate a follow-up that edits its output in place, like a database migration on top of a schema:

```bash
# T005 already implemented the login endpoint. New requirement: add rate-limiting.
hybrid-agent generate-tasks -c config.yaml \
  -f "Add rate limiting to the login endpoint" \
  --update T005

hybrid-agent run -c config.yaml -t tasks.md --auto-resume 5
```

What `--update T005` does:

- Implies `--append` and validates that `T005` actually exists in `tasks.md`.
- Tells Claude the new task is a migration on top of `T005`: each new task it emits has `depends_on: [T005]` and includes `migration` in its `tags`.
- At run time, the orchestrator notices the `migration` tag and **forks the new task's sandbox from `agent/T005`'s branch** instead of `main`. The worker therefore opens its sandbox with `T005`'s code already in place and edits it in place — no full rewrite, no missing context.
- `merge` later picks up the migration task's branch (topo-latest), so the final output reflects T005 + the delta.

This is the idiomatic way to layer a change onto already-shipped work without losing what was there.

### Re-doing an old task from scratch

Sometimes the right move *is* to throw away `T005`'s output and rebuild it. That's the `reset` workflow, not the migration workflow:

```bash
hybrid-agent reset -c config.yaml --task-id T005
hybrid-agent run -c config.yaml -t tasks.md --auto-resume 5
```

Use migration when the old code is mostly right and needs an additive change. Use reset when the old design itself is wrong.

## Task schema

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Stable unique identifier (e.g. `T001`) |
| `title` | yes | Short title |
| `description` | no | Free text |
| `depends_on` | no | List of task ids; this task waits for all to be `DONE` |
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

## Routing rules

Defined in `routing.py`, evaluated in order:

1. **AvailabilityRule** — if local is unhealthy, route to Claude (and vice versa)
2. **ForceTagRule** — `force_claude_tags` or `force_local_tags` from config
3. **FailureEscalationRule** — after N local failures, escalate to Claude
4. **PathGlobRule** — glob match on `target_files` → Claude
5. **ComplexityRule** — `complexity: high` → Claude
6. **Default** — falls through to `default_backend`

To add a custom rule, implement the `RoutingRule` protocol and insert it in `build_default_router()`.

## Test gate (reviewer runs pytest)

When `orchestrator.run_tests_in_review = true` (default), the reviewer runs the project's tests after its LLM-driven verdict. Anything other than `PASSED` forces `NEEDS_FIX` and appends the test output to the issue list — the worker then gets re-run with that feedback.

Auto-detected by marker files in the sandbox root:

| Marker | Language | Default command |
|--------|----------|-----------------|
| `pyproject.toml`, `setup.py`, `requirements.txt` | Python | `pytest -x --tb=short` |
| `package.json` | Node.js | `npm test --silent` |
| `go.mod` | Go | `go test ./...` |
| `Cargo.toml` | Rust | `cargo test --quiet` |
| `pom.xml` | Java (Maven) | `mvn -q test` |
| `build.gradle*` | Java/Kotlin (Gradle) | `gradle test --quiet` |
| `Gemfile` | Ruby | `bundle exec rspec` |
| `composer.json` | PHP | `vendor/bin/phpunit` |
| `mix.exs` | Elixir | `mix test` |

For mixed-language projects or custom workflows, set `orchestrator.test_command` explicitly. Setting `require_tests: true` makes a sandbox without any tests fail review even when the LLM approves.

### Post-merge LLM semantic review (Tier B, opt-in)

Pass `--llm-review` along with `--review` to add one Claude call that audits the merged tree for problems the deterministic gates can't see — the things that bite *after* every per-task pytest passes:

- **Stale references** — `login/page.tsx` still imports `loginUser()` from `lib/api.ts`, but a later task renamed it to `signIn()`.
- **Logic conflicts** — two tasks defined `<Toast>` in conflicting ways and the topo-late version dropped the dismiss callback the earlier version had.
- **Missing wiring** — `tasks.md` says T019 ships a "/students" route, the file exists, but nothing in the router actually mounts it.
- **Cross-cutting drift** — `README.md` mentions a feature that was never implemented; `.env.example` has keys the code doesn't read.

**Mode** (`--llm-review-mode`):

- `inline` (default, recommended) — every conflict file is pre-extracted into the prompt (capped per-file and total) and Claude is told **not** to call tools. Fast, deterministic, and avoids the Windows Claude-CLI subprocess flake (`Command failed with exit code 1` mid-tool-use) that can otherwise burn 10+ minutes of retries on a big tree.
- `tools` — passes `cwd=merged/` and allows `Read`/`Grep`/`Glob` so Claude can explore beyond the conflict set. More thorough on large projects with deep transitive dependencies between non-conflict files. Use when inline mode says it elided files for size, or when you specifically want to follow a chain like `page.tsx → utils.ts (single-author) → types.ts (single-author)`.

**Auto-fallback** (default on): if `--llm-review-mode tools` exhausts its retries (typical Windows flake), the gate transparently retries once in inline mode rather than dropping the entire review. The fallback review's body is tagged `_(fallback: tools mode flaked, this review came from inline mode)_` so you can tell it apart. Disable with `--no-llm-fallback` if you want the failure to surface as-is.

Retry budget for the LLM review is intentionally small: 2 attempts with 5–30s backoff and a 180s per-call timeout. A flake costs you ~6 minutes max, not 10. With auto-fallback, worst case is ~6 minutes (tools) + ~1 minute (inline).

Inputs to the model: full `tasks.md` content, the conflict log (which file was written by which sequence of tasks), plus either embedded file contents (inline) or `Read`/`Grep`/`Glob` over the merged tree (tools). The model returns a strict-format response:

```
Verdict: PASS | WARN | FAIL
- [CRITICAL] frontend/src/app/login/page.tsx imports `loginUser` from
  @/lib/api, but lib/api.ts:42 only exports `signIn`. Login is broken.
- [HIGH] frontend/src/contexts/AuthContext.tsx no longer wraps children
  with <ToastProvider>; toasts from Tier C tasks won't render.
- [LOW] README.md still references the deprecated /api/v1 path.
```

`FAIL` is a hard failure (exits non-zero). `WARN` and `PASS` print but don't block. If Claude itself errors out (network, rate limit), the review is downgraded to a warning — the deterministic gates already ran.

Cost: typically one Sonnet call, $0.05–$0.30 depending on how much the model decides to read. Skip on small projects, run on integration milestones.

### Auto-fix on FAIL (`--llm-fix`)

`--llm-fix` extends the LLM review with an automatic remediation loop: when the verdict is `FAIL`, instead of exiting, Claude is given `Read`/`Edit`/`Write`/`Grep`/`Glob` access to the merged tree and asked to apply minimal fixes to the flagged issues. After the fix lands, the review re-runs (forced inline mode for reliability) to verify. If the new verdict is still `FAIL`, another fix iteration is launched — up to `--llm-fix-iterations` rounds (default 3). The gate hard-fails only when the loop exits with verdict still `FAIL`.

This loop matters because **LLM review is non-exhaustive**: each pass surfaces a different layer of issues based on what the model focuses on. Round 1 might catch the broken auth flow; round 2 catches a `.ts` file that should be `.tsx`; round 3 catches a missing config. Without iteration, the gate would FAIL after the first fix even though half the issues got resolved.

Each iteration also embeds **files the previous fix touched** (snapshot via mtime), including newly-created single-author files like `tailwind.config.ts` that wouldn't appear in the original conflict-driven embed set. Without this, the post-fix review can't verify configs the fix just created.

**Fix mode** (`--llm-fix-mode`):

- `patch` (default, recommended) — Claude receives the embedded files + reviewer issues and replies with a JSON patch in a `<patch>...</patch>` block. The CLI parses it and applies `edit`/`write`/`delete` ops deterministically in Python. **No tool calls** — same trick as `--llm-review-mode inline`, sidesteps the Windows Claude-CLI subprocess flake that crashes long-running tool queries (~3 min mark on big trees).
- `tools` — Claude gets `Read`/`Edit`/`Write`/`Grep`/`Glob` access on the merged tree and edits in place. More flexible (it can grep around for callers before fixing) but unreliable on Windows for big merges. Use only when patch mode keeps misfiring on `find` strings that aren't unique enough.

Patch ops:

```json
[
  {"op": "edit",   "path": "rel/path", "find": "<exact existing text>", "replace": "<new text>"},
  {"op": "write",  "path": "rel/path", "content": "<full file content>"},
  {"op": "delete", "path": "rel/path"}
]
```

`edit` requires the `find` string to appear **exactly once** in the target file — Claude is told to include enough surrounding context (a unique import line or function signature) for unambiguous matching. Path traversal outside the merged tree is blocked. Failures (find string missing, ambiguous match, file not found) are surfaced as per-op errors in the fix summary so the next iteration's review picks them up.

What the fix pass is allowed to do:

- Edit existing files cited in the issues.
- Create missing config files when an issue mandates them (e.g. add `tailwind.config.js` + `postcss.config.js` when the reviewer flagged "Tailwind toolchain missing").
- Populate empty docs (e.g. an empty `README.md`) using the `tasks.md` context.
- Add new dependencies to `package.json` / `requirements.txt` / `pyproject.toml` when an issue demands them (note: it does *not* run install — that's on you).

What it does **not** do:

- Run shell commands. Bash is excluded from the allow-list. No `npm install`, no `git commit`, no migrations.
- Touch files unrelated to the flagged issues.
- Fix `[LOW]` issues unless the change is one or two lines.
- Resolve genuinely ambiguous design questions — instead, it picks the option closest to the spec referenced in the issue and leaves a comment noting the choice.

Output structure (round 1 fixes most issues, round 2 catches a file the first review missed):

```
FAIL llm review (initial) verdict: FAIL
--- llm review (initial) ---
- [CRITICAL] AuthContext.tsx calls /auth/me but no such endpoint exists
- [HIGH] frontend/package.json missing tailwindcss/postcss/autoprefixer
- ...
--- end review ---
Verdict FAIL — running --llm-fix iteration 1/3…
--- llm fix summary (iter 1) ---
## Files changed
- frontend/src/contexts/AuthContext.tsx: removed /auth/me call, derived
  user from token claims instead
- frontend/package.json: added tailwindcss/postcss/autoprefixer
- frontend/tailwind.config.js: created with content globs for src/
--- end fix summary ---
fix touched 5 file(s); 2 new in embed set
re-running review after iter 1…

FAIL llm review (after iter 1) verdict: FAIL
--- llm review (after iter 1) ---
- [CRITICAL] frontend/src/hooks/useToast.ts contains JSX but has .ts ext
--- end review ---
Verdict FAIL — running --llm-fix iteration 2/3…
--- llm fix summary (iter 2) ---
## Files changed
- frontend/src/hooks/useToast.tsx: renamed from .ts (now parses)
--- end fix summary ---
re-running review after iter 2…

OK llm review (after iter 2) verdict: PASS
```

Cost: each iteration is roughly 2 Claude calls (fix + review). With default 3 iterations: up to 7 calls (initial review + 3×fix + 3×review). Total typically $0.30–$1.50. The loop exits early on `PASS`/`WARN`, so most runs cost less. Use on integration milestones where you want the gate to actively repair drift.

### Post-merge system test (Tier F)

`merge --review` adds a second test pass on the merged tree, distinct from per-task unit tests, to confirm the integrated project actually works end-to-end. The command is resolved in this order:

1. Explicit override: `orchestrator.system_test_command` (most flexible — supports compound commands like `"docker compose up -d && pytest tests/system && docker compose down"`).
2. Auto-detection on the merged output:
   - `tests/integration/` → `pytest tests/integration -x --tb=short`
   - `tests/e2e/` → `pytest tests/e2e -x --tb=short`
   - `tests/system/` → `pytest tests/system -x --tb=short`
   - `integration_tests/` → `pytest integration_tests -x --tb=short`
   - `e2e/` with a `test:e2e` / `e2e` / `integration` script in `package.json` → `npm run <script> --silent`
   - `Makefile` with a `test-system` / `system-test` / `e2e` / `test-e2e` / `integration` target → `make <target>`
3. Nothing matched and nothing configured: skip (printed as `system test: not configured`). The review gate still passes — the project just doesn't have a system test yet.

Timeout is `system_test_timeout_seconds` (default 900s) — separate from the unit-test timeout because system tests usually spin up servers, hit databases, or run browsers. Hard fail on `FAILED` / `TIMEOUT` / `ERROR`; soft skip on `NO_TESTS` (configured but the suite collected nothing).

## Cost tracking

Every LLM call funnels through a `CostMeter` bound to the run. Token counts come from the provider response (Ollama and Claude SDK both expose them); Claude is priced at the published $/1M list; local models default to `$0.00` (override via `cost.pricing_overrides` if you want to attribute electricity).

Caps are enforced at every call:

- `per_task_usd_cap` — exceeded → task fails (or warns)
- `per_run_usd_cap` — exceeded → whole run stops (or warns)
- `on_exceed: stop` raises `BudgetExceededError` (treated as fatal by retry — no retries on a budget hit)
- `on_exceed: warn` just logs

Run summary is printed at end of every run (and every crash, in `finally`):

```
Cost summary:
  total: $0.1123 (prompt=61500, completion=15700, calls=4)
  by model:
    claude-sonnet-4-5: $0.1050 (10000+5000 tok, 1 calls)
    qwen3-coder:30b:   $0.0000 (50000+10000 tok, 1 calls)
  by task:
    T001: $0.1050 (10000+5000 tok, 1 calls)
```

## Structured logging

Every log line carries `run_id`, `task_id`, `stage`, `attempt` automatically — they're bound to a contextvar at the pipeline boundary and propagate through `await` and `asyncio.create_task`. Existing `logging.getLogger(__name__)` calls keep working — the orchestrator wraps them in the same handler chain.

Two sinks:

- **Console** (always): RichHandler, colored, with all context fields appended.
- **JSON file** (optional): one JSON object per line, ready for Loki/ELK/CloudWatch. Set `log_json_file` in config to enable.

Example JSON entry:

```json
{"event": "Task T002 routed to local (default)", "run_id": "94ffd664",
 "task_id": "T002", "stage": "code", "attempt": 1, "level": "info",
 "logger": "hybrid_agent.orchestrator", "timestamp": "2026-05-07T13:37:55Z"}
```

## Resume behavior

The orchestrator treats SQLite as the source of truth. State transitions persist on every step.

On startup, any task in a transient or intermediate status is rolled back to `READY`:

| From | To |
|------|-----|
| `PLANNING`, `CODING`, `REVIEWING`, `FIXING` | `READY` |
| `PLANNED` (plan saved, never resumed) | `READY` |
| `CODED` (code saved, never reviewed) | `READY` |
| `BLOCKED`, when all deps are now `DONE` | `READY` (auto-revived) |

The pipeline body checks `if ex.plan is None` and `if ex.code is None` — so a task with both already saved skips planning + coding and goes straight to review on resume. Already-committed work in the sandbox is preserved.

Tasks in `DONE` and `FAILED` are not retried automatically. `BLOCKED` tasks are auto-revived on the next run *if* their failed dependencies have since gone DONE — so a successful resume of an upstream task frees its dependents without needing a manual `reset`.

For transient flakes that take more than one run to clear, prefer `hybrid-agent run --auto-resume N`: it wraps `run` in a loop that resets `FAILED + BLOCKED`, re-runs, and stops when everything is `DONE` or no progress is made between rounds. `hybrid-agent reset` is still available for surgical retries (single task id, or wiping `FAILED`/`BLOCKED` back to `READY` manually).

The sandbox directory is reused across runs — if it exists, we skip recreation. Delete `<project_root>/.hybrid_agent_sandboxes/<task_id>` to force a fresh one.

## Concurrency model

With **12 GB VRAM** you can run **one** Qwen3-Coder 30B endpoint. The orchestrator therefore enforces:

- Local pool concurrency = number of endpoints in `local.endpoints` (default 1)
- Claude pool concurrency = `claude.concurrency` (default 1)
- Task-level concurrency = `orchestrator.max_concurrent_tasks` (default 4)

While the local LLM serves task A, task B can be reading files / running tests / waiting on Claude — that's where the parallelism comes from. To get true LLM-level parallelism, add more endpoints to the `local.endpoints` list (another machine running Ollama, or a vLLM cluster).

## Failure handling

- **Per-call retry**: each LLM call retries up to `retry.max_attempts` with exponential backoff. Auth/4xx errors are classified as fatal and don't retry. Budget-exceeded errors are also fatal.
- **Per-task timeout**: `plan_timeout_seconds`, `code_timeout_seconds`, `review_timeout_seconds` — each scaled at run time by `complexity_timeout_multiplier[<task.complexity>]`. Timeout errors record which stage timed out and what budget it had (e.g. `timeout in stage=code after 3600s (complexity=high)`), not just an empty `timeout:`.
- **Cross-run auto-resume**: `hybrid-agent run --auto-resume N` reruns until all tasks are DONE or no progress is made — useful for transient subprocess flakes that take more than one round to clear.
- **Review iterations**: if reviewer says `needs_fix`, the worker re-runs with the issue list appended to the plan, up to `review_max_iterations`.
- **Test gate**: failing pytest forces `needs_fix` regardless of LLM verdict. Timeout kills the test process tree (works correctly on Windows via `taskkill /F /T`).
- **Graceful shutdown**: SIGINT/SIGTERM cancels in-flight tasks, persists state, and exits.
- **Crash recovery**: on next run, in-progress + intermediate tasks are rolled back to `READY`, and `BLOCKED` tasks whose deps have since gone DONE are auto-revived (see Resume above).

## Development

The repo ships with a comprehensive test suite (310 tests, ~94% coverage) and a local-CI script that runs lint + format + types + tests in one command.

```bash
# Run everything (lint + format check + mypy + pytest with coverage gate)
./scripts/check.sh           # Linux/macOS/WSL
.\scripts\check.ps1          # Windows PowerShell

# Or each step individually
ruff check hybrid_agent/         # lint
ruff format hybrid_agent/        # auto-format
mypy hybrid_agent/               # type check
pytest                           # tests
pytest --cov=hybrid_agent        # tests + coverage gate (≥80% enforced)
```

Test layout:

```
tests/
├── conftest.py                  # FakeLLMClient/Pool, base_config, factories
├── unit/                        # ~80% of tests, pure-logic coverage
│   ├── test_dag.py / test_routing.py / test_retry.py / ...
│   ├── agents/test_{planner,worker,reviewer}.py
│   └── llm/test_{ollama,claude}.py
└── integration/
    └── test_orchestrator_e2e.py # full plan → code → review with mocked LLMs
```

LLMs are mocked end-to-end via `FakeLLMClient` (`tests/conftest.py`) — the suite runs in ~6 seconds without hitting Ollama or the Claude SDK.

## File map

```
hybrid_agent/
├── pyproject.toml
├── config.example.yaml
├── tasks.example.md
├── tasks.example.yaml
├── README.md
├── scripts/
│   ├── check.ps1               # local CI: ruff + mypy + pytest
│   └── check.sh
├── tests/
│   ├── conftest.py             # shared fixtures + FakeLLM
│   ├── unit/
│   └── integration/
└── hybrid_agent/
    ├── __main__.py             # python -m hybrid_agent
    ├── cli.py                  # click commands (run/status/show/reset/merge/generate-tasks)
    ├── config.py               # Pydantic config models
    ├── models.py               # TaskSpec, TaskExecution, enums, outputs
    ├── state.py                # SQLite store + resume logic
    ├── dag.py                  # dependency graph
    ├── retry.py                # retry policy + error classification
    ├── sandbox.py              # git worktree manager (with copy fallback)
    ├── routing.py              # pluggable routing rules
    ├── orchestrator.py         # main async loop
    ├── cost.py                 # CostMeter + budget caps + pricing table
    ├── logging_config.py       # structlog setup + trace_context
    ├── language.py             # project-language detection from marker files
    ├── test_runner.py          # pytest gate (subprocess + classify + kill_tree)
    ├── task_generator.py       # NL/SRS → tasks.md generator
    ├── prompts/
    │   └── task_generator.md   # skill prompt (standalone, reusable)
    ├── parsers/                # markdown / yaml_json / csv / excel / word
    ├── llm/                    # base / ollama / claude
    └── agents/                 # planner / worker / reviewer
```

## Limitations to be honest about

- **30B local model has a real failure rate** on agentic coding (≥30% rework expected on a mature codebase). The reviewer's auto-fix authority and `escalate_to_claude_after_failures` exist to compensate, but expect to babysit early on.
- **A 12 GB VRAM single-endpoint setup means LLM calls are serial.** Adding more endpoints (more machines / cloud GPUs) is the way to scale.
- **No production-grade sandbox isolation.** Generated code runs directly on the host. This tool is meant for personal development workflows where you review the output before running it. For untrusted code, wrap `test_runner.run_tests` and the worker file writes in Docker yourself.
- **Resume is best-effort.** If the process crashed mid-write to the sandbox filesystem, you may need to manually inspect the worktree.
- **Token-count accuracy depends on the SDK.** Ollama returns exact counts. Claude SDK sometimes doesn't surface usage on every message — we fall back to a 4-chars-per-token estimate, which is within ~10% on English text.

## License

MIT.
